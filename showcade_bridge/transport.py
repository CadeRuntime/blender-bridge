# SPDX-FileCopyrightText: 2026 caderuntime <caderuntime@cade.run>
# SPDX-License-Identifier: GPL-3.0-or-later

"""transport.py — the asset-catalog HTTP client for the Blender addon.

**This module imports no `bpy`, on purpose.** That is the entire strategy for
testing the addon without Blender: `test/` runs it under plain
`unittest` against a catalog on an ephemeral port
(`task test:blender`). Keep it that way — no `bpy`, no `requests`, stdlib only.

The transport contract is read out of the published API reference
<https://cade.run/api/assets-http/>, not
assumed (ADR 0007 "Transport facts"):

1. ``HEAD {endpoint}/blob/{sha256}`` → 200 (blob present) or 404. Unauthenticated.
2. ``POST {endpoint}`` with ``Authorization: Bearer …`` and
   **multipart/form-data only** — `handleUpload` calls `req.formData()` and 400s
   otherwise; there is no raw-body POST. Part classification is load-bearing:

   * ``meta`` — a JSON string with **no filename and no Content-Type**. A part
     without a filename decodes to a *string* entry, which is what
     ``typeof metaRaw !== "string"`` demands.
   * ``file`` — the GLB **with** a filename; that is what makes it a ``Blob``.
     Omitted entirely when step 1 said 200, with ``sha256`` carried in ``meta``
     (the metadata-only dedupe path).

   → 201 with the new row, or 200 with the existing row (dedupe is by
   ``(sha256, name)``). A 200 is NOT necessarily write-free: since
   showcade-4xwb the service UNIONS the incoming tags onto the matched row, so
   a re-send carrying a new tag does write, and can 400 when the merged tag set
   exceeds the catalog's MAX_TAGS. Re-sending tags the row already has stays
   write-free (bd showcade-7x2t).
3. ``DELETE {endpoint}/{id}`` → 204, reclaiming the blob once no live row
   references it. The only storage-reclaim lever a hot-reload loop has.

CORS is **not** a factor: server-to-server sends no ``Origin``, the service's
``corsOriginFor`` returns null without one, and ``urllib`` never preflights.
Nothing to configure — do not touch ``DEV_CORS_ORIGINS`` for this.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import ssl
import urllib.error
import urllib.request
from collections.abc import Iterable, Mapping, Sequence
from typing import NamedTuple

__all__ = [
    "DEFAULT_ENDPOINT",
    "DEFAULT_TIMEOUT_S",
    "LINK_TAG_PREFIX",
    "MAX_MODEL_BYTES",
    "MAX_NAME_LEN",
    "MAX_TAGS",
    "MAX_TAG_LEN",
    "ProbeResult",
    "Response",
    "TransportError",
    "UploadResult",
    "blob_exists",
    "credential",
    "delete_asset",
    "encode_multipart",
    "hint_for",
    "link_tag",
    "model_tags",
    "new_link_id",
    "probe",
    "sha256_hex",
    "upload_model",
]

DEFAULT_ENDPOINT = "http://localhost:8787/api/assets"
DEFAULT_TIMEOUT_S = 30.0

# The service's own cap — mirrored so the addon can fail with a
# useful message instead of shipping bytes it knows will be rejected.
MAX_MODEL_BYTES = 32 * 1024 * 1024
MAX_NAME_LEN = 120
MAX_TAGS = 16
MAX_TAG_LEN = 40

#: Stable identity across re-sends is a catalog TAG (ADR 0007 decision 1).
#: It must be a SHORT token: "blender:" + a 36-char uuid is 44 chars and blows
#: MAX_TAG_LEN=40. Eight random bytes hex is 24 chars total.
LINK_TAG_PREFIX = "blender:"
_LINK_ID_BYTES = 8

# every one of them ({{{...}}}) and the regex stops being readable at a glance.
_LINK_ID_RE = re.compile(r"^[0-9a-f]{%d}$" % (_LINK_ID_BYTES * 2))  # noqa: UP031

#: The addon never bakes a scale — glTF's unit IS the metre and only the browser
#: knows the target device footprint (ADR 0007 decision 2). This tag records
#: that contract on the row.
UNITS_TAG = "units:m"
SOURCE_TAG = "blender"

_GLB_MIME = "model/gltf-binary"


class TransportError(RuntimeError):
    """A transport-level failure with the operator-facing fix attached.

    ``status`` is the HTTP status when the server answered, else ``None`` (the
    request never completed — wrong port, service down, TLS).
    """

    def __init__(self, message: str, *, status: int | None = None, hint: str | None = None):
        super().__init__(message)
        self.status = status
        self.hint = hint or hint_for(status)

    def __str__(self) -> str:  # pragma: no cover - formatting only
        base = super().__str__()
        return f"{base} — {self.hint}" if self.hint else base


_HINTS: Mapping[int, str] = {
    400: "the catalog rejected the metadata; the server's message above is verbatim",
    401: (
        "not authorized — if you signed in, the session has expired (Sign in again); "
        "otherwise check the upload token in the addon preferences"
    ),
    403: "that endpoint is read-only (no upload token configured) — you are probably on prod",
    404: "no such asset at that endpoint",
    413: f"the GLB is over the {MAX_MODEL_BYTES // (1024 * 1024)} MB cap — decimate, or drop 4K textures",
    415: "the bytes are not a recognized model — export_format must be GLB",
    507: "the catalog hit its storage ceiling — delete some assets",
}


def credential(session_token: str | None, upload_token: str | None) -> str:
    """The bearer value a write should carry: the SESSION token when signed in, else the
    static upload token.

    Two credentials coexist because two eras do (bd showcade-fmq8). A deployment with
    `AUTH_SESSION_URL` set wants a Better Auth session; one without it wants the shared
    `ASSETS_UPLOAD_TOKEN`. The addon does NOT probe which era an endpoint is in — a probe
    would cost a round-trip on every send and still race a config change — so it prefers the
    more specific credential and lets a mismatch surface as the 401 it is.

    Session-first, deliberately: the static token is the DEFAULT ('dev' out of the box), so
    preferring it would mean a signed-in user silently kept sending the wrong credential.
    """
    return (session_token or "").strip() or (upload_token or "").strip()


def hint_for(status: int | None) -> str:
    """Map a failure onto the user's next action, not the status code."""
    if status is None:
        return "could not reach the catalog — is `task dev:assets` running on that endpoint?"
    return _HINTS.get(status, "unexpected catalog response")


# ── hashing / identity ───────────────────────────────────────────────────


def sha256_hex(data: bytes) -> str:
    """Lowercase hex sha256 — the catalog's content address."""
    return hashlib.sha256(data).hexdigest()


def new_link_id() -> str:
    """Mint a fresh link id (16 hex chars). Stored in the .blend, stamped on
    every upload, and how the browser finds the current bytes."""
    return secrets.token_hex(_LINK_ID_BYTES)


def link_tag(link_id: str) -> str:
    """``blender:<16-hex>`` — validated, because an over-long or malformed tag
    is a 400 from the server and a confusing one at that."""
    if not _LINK_ID_RE.match(link_id):
        raise ValueError(f"link id must be {_LINK_ID_BYTES * 2} lowercase hex chars, got {link_id!r}")
    tag = LINK_TAG_PREFIX + link_id
    assert len(tag) <= MAX_TAG_LEN, "link tag must fit MAX_TAG_LEN"
    return tag


def model_tags(link_id: str | None = None, extra: Iterable[str] = ()) -> list[str]:
    """The tag set every Blender upload carries: provenance, the units contract,
    and (when linked) the stable identity."""
    tags = [SOURCE_TAG, UNITS_TAG]
    if link_id is not None:
        tags.append(link_tag(link_id))
    for tag in extra:
        tag = tag.strip()
        if tag and tag not in tags:
            tags.append(tag)
    if len(tags) > MAX_TAGS:
        raise ValueError(f"at most {MAX_TAGS} tags, got {len(tags)}")
    for tag in tags:
        if len(tag) > MAX_TAG_LEN:
            raise ValueError(f"tag {tag!r} exceeds {MAX_TAG_LEN} chars")
    return tags


# ── multipart ────────────────────────────────────────────────────────────


class FilePart(NamedTuple):
    name: str
    filename: str
    content_type: str
    data: bytes


def _header_safe(value: str) -> str:
    """Strip anything that could break out of the Content-Disposition header.
    A quote or a CRLF in a filename would forge a part boundary."""
    cleaned = re.sub(r'[\r\n"\\]', "_", value).strip()
    return cleaned or "upload.bin"


def encode_multipart(
    fields: Mapping[str, str],
    files: Sequence[FilePart] = (),
    *,
    boundary: str | None = None,
) -> tuple[str, bytes]:
    """Hand-roll a multipart/form-data body. Returns ``(content_type, body)``.

    Plain fields get **no filename and no Content-Type** so the service decodes
    them as strings; file parts get both so they decode as Blobs.
    """
    boundary = boundary or "----showcade" + secrets.token_hex(16)
    marker = boundary.encode("utf-8")
    if any(boundary in value for value in fields.values()) or any(marker in part.data for part in files):
        # Astronomically unlikely with a random boundary, but silent body
        # corruption is the failure mode, so it is worth one comparison.
        raise ValueError("boundary collides with the payload")
    sep = f"--{boundary}\r\n".encode("utf-8")
    out = bytearray()
    for name, value in fields.items():
        out += sep
        out += f'Content-Disposition: form-data; name="{_header_safe(name)}"\r\n\r\n'.encode("utf-8")
        out += value.encode("utf-8")
        out += b"\r\n"
    for part in files:
        out += sep
        out += (
            f'Content-Disposition: form-data; name="{_header_safe(part.name)}";'
            f' filename="{_header_safe(part.filename)}"\r\n'
        ).encode("utf-8")
        out += f"Content-Type: {part.content_type}\r\n\r\n".encode("utf-8")
        out += part.data
        out += b"\r\n"
    out += f"--{boundary}--\r\n".encode("utf-8")
    return f"multipart/form-data; boundary={boundary}", bytes(out)


# ── HTTP ─────────────────────────────────────────────────────────────────


class Response(NamedTuple):
    status: int
    body: bytes

    def json(self) -> dict:
        try:
            parsed = json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}

    @property
    def error(self) -> str:
        return str(self.json().get("error") or f"HTTP {self.status}")


def _ssl_context(insecure_tls: bool) -> ssl.SSLContext | None:
    if not insecure_tls:
        return None
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def request(
    method: str,
    url: str,
    *,
    body: bytes | None = None,
    content_type: str | None = None,
    token: str | None = None,
    timeout: float = DEFAULT_TIMEOUT_S,
    insecure_tls: bool = False,
) -> Response:
    """One request, no retries. A non-2xx is a `Response`, not an exception —
    callers branch on the status (404 from HEAD is normal, and 401 vs 403 mean
    different fixes). Only a request that never completed raises."""
    req = urllib.request.Request(url, data=body, method=method)
    if content_type:
        req.add_header("Content-Type", content_type)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_ssl_context(insecure_tls)) as resp:
            return Response(resp.status, resp.read())
    except urllib.error.HTTPError as exc:  # the server answered, just not 2xx
        try:
            return Response(exc.code, exc.read())
        finally:
            exc.close()  # HTTPError holds an open socket until read AND closed
    except urllib.error.URLError as exc:
        raise TransportError(f"{method} {url} failed: {exc.reason}") from exc
    except TimeoutError as exc:
        raise TransportError(f"{method} {url} timed out after {timeout:g}s") from exc


def blob_exists(
    endpoint: str,
    sha256: str,
    *,
    timeout: float = DEFAULT_TIMEOUT_S,
    insecure_tls: bool = False,
) -> bool:
    """Step 1 of the dedupe handshake. Unauthenticated by design."""
    resp = request(
        "HEAD",
        f"{endpoint.rstrip('/')}/blob/{sha256}",
        timeout=timeout,
        insecure_tls=insecure_tls,
    )
    if resp.status in (200, 404):
        return resp.status == 200
    raise TransportError(f"HEAD blob/{sha256[:12]}… returned {resp.status}", status=resp.status)


class UploadResult(NamedTuple):
    status: int
    """201 = a new catalog row; 200 = the identical (sha256, name) already existed."""
    asset: dict
    """The AssetMeta row the service returned."""
    sent_bytes: int
    """0 when the blob was already stored and only metadata crossed the wire."""

    @property
    def asset_id(self) -> str:
        return str(self.asset.get("id", ""))

    @property
    def created(self) -> bool:
        return self.status == 201

    @property
    def deduped(self) -> bool:
        """True when nothing was written — same bytes, same name."""
        return self.status == 200


def upload_model(
    endpoint: str,
    token: str,
    *,
    name: str,
    data: bytes,
    tags: Sequence[str] = (),
    filename: str | None = None,
    timeout: float = DEFAULT_TIMEOUT_S,
    insecure_tls: bool = False,
) -> UploadResult:
    """Upload a GLB, skipping the bytes when the catalog already has that blob.

    The name is kept stable across re-sends on purpose: dedupe is by
    ``(sha256, name)``, so an unchanged re-export returns 200 with the existing
    row and writes nothing — idempotency for free.
    """
    name = name.strip()
    if not name or len(name) > MAX_NAME_LEN:
        raise ValueError(f"name must be 1..{MAX_NAME_LEN} chars, got {len(name)}")
    if not data:
        raise ValueError("refusing to upload an empty GLB")
    # The size cap is enforced HERE, not just admired (bd showcade-81il). The
    # constant above says it is "mirrored so the addon can fail with a useful
    # message instead of shipping bytes it knows will be rejected", but nothing
    # checked it: a 32 MB+ export was hashed, copied into the multipart buffer
    # and pushed over the wire in full, only to come back 413. Same message the
    # 413 hint carries, raised before any of that work — and, like the other
    # local guards above, a ValueError, because no server answered.
    if len(data) > MAX_MODEL_BYTES:
        # Exact bytes, not rounded MB: 32.0 MB over a "32 MB cap" reads like a
        # rounding bug rather than a refusal.
        raise ValueError(
            f"the GLB is {len(data):,} bytes, over the "
            f"{MAX_MODEL_BYTES // (1024 * 1024)} MB cap — decimate, or drop 4K textures"
        )

    sha256 = sha256_hex(data)
    # Annotated, not inferred: the literal alone infers dict[str, str], and `tags`
    # is a LIST — the one heterogeneous value in an otherwise all-string payload.
    # This is the JSON meta part the service parses, so `object` is the honest
    # element type rather than a workaround for the assignment below.
    meta: dict[str, object] = {"name": name, "kind": "model", "sha256": sha256}
    if tags:
        meta["tags"] = list(tags)

    have_blob = blob_exists(endpoint, sha256, timeout=timeout, insecure_tls=insecure_tls)
    files: tuple[FilePart, ...] = ()
    if not have_blob:
        files = (FilePart("file", filename or f"{name}.glb", _GLB_MIME, data),)

    content_type, body = encode_multipart({"meta": json.dumps(meta)}, files)
    resp = request(
        "POST",
        # rstrip like every other call site (blob_exists / stats / the token
        # probe / delete_asset): the upload route is an EXACT `^…/api/assets$`
        # match, so a configured endpoint with a trailing slash 404s here — and
        # hint_for(404) then blames a missing asset id (bd showcade-xlly).
        endpoint.rstrip("/"),
        body=body,
        content_type=content_type,
        token=token,
        timeout=timeout,
        insecure_tls=insecure_tls,
    )
    # Metadata-only, and the catalog says it has never heard of that blob (bd
    # showcade-u4nw). The two stores can legitimately disagree: `blob_exists`
    # probes the BLOB STORE (handleBlobHead -> deps.blobs.head) while a
    # metadata-only POST needs the CATALOG's blob row (deps.catalog.getBlobRow),
    # so a crash between blobs.put and ensureBlob — or a concurrent delete
    # between our HEAD and this POST — leaves the HEAD true and the POST 404.
    # Raising here would blame the endpoint (hint_for(404) = "no such asset at
    # that endpoint"), which is the wrong fix and not something the operator can
    # act on. Send the bytes once instead: we are holding them, and it heals the
    # divergence. A 404 with the bytes already attached is genuinely unroutable
    # and falls through to the raise below.
    sent_bytes = 0 if have_blob else len(data)
    if resp.status == 404 and have_blob:
        files = (FilePart("file", filename or f"{name}.glb", _GLB_MIME, data),)
        content_type, body = encode_multipart({"meta": json.dumps(meta)}, files)
        resp = request(
            "POST",
            endpoint.rstrip("/"),
            body=body,
            content_type=content_type,
            token=token,
            timeout=timeout,
            insecure_tls=insecure_tls,
        )
        sent_bytes = len(data)
    if resp.status not in (200, 201):
        raise TransportError(f"upload of {name!r} failed: {resp.error}", status=resp.status)
    return UploadResult(resp.status, resp.json(), sent_bytes)


class ProbeResult(NamedTuple):
    """Whether this endpoint+token could actually accept an upload."""

    ok: bool
    message: str
    """One line for the operator's report — the fix, not the status code."""
    assets: int = 0
    bytes: int = 0


#: A syntactically valid asset id (``[0-9A-Za-z_-]{1,64}``, so the PATCH route
#: matches) that cannot collide with a real one — catalog ids are ULIDs, which
#: are 26 chars of Crockford base32 and never contain a '-'.
PROBE_ASSET_ID = "showcade-connection-probe"


def probe(
    endpoint: str,
    token: str,
    *,
    timeout: float = DEFAULT_TIMEOUT_S,
    insecure_tls: bool = False,
) -> ProbeResult:
    """Check reach **and** the token without writing anything.

    Two requests, because they answer different questions and fail differently:

    1. ``GET /stats`` — unauthenticated, so a failure here is purely "is the
       catalog there" (wrong port, service down, TLS).
    2. ``PATCH /<probe-id>`` with an empty JSON patch — ``handlePatch``
       authorizes **before** it looks the asset up (`handler.ts:405` then
       `:417`), so **401 = bad token** and **404 = the token is good**. Nothing
       is created, renamed or deleted on any branch.
    """
    try:
        stats = request(
            "GET", f"{endpoint.rstrip('/')}/stats", timeout=timeout, insecure_tls=insecure_tls
        )
    except TransportError as exc:
        return ProbeResult(False, str(exc))
    if stats.status != 200:
        return ProbeResult(False, f"{endpoint} answered {stats.status} for /stats — {hint_for(stats.status)}")
    totals = stats.json()

    try:
        auth = request(
            "PATCH",
            f"{endpoint.rstrip('/')}/{PROBE_ASSET_ID}",
            body=b"{}",
            content_type="application/json",
            token=token,
            timeout=timeout,
            insecure_tls=insecure_tls,
        )
    except TransportError as exc:
        return ProbeResult(False, str(exc))
    if auth.status != 404:
        return ProbeResult(
            False, f"the catalog is reachable but the token is not usable: {hint_for(auth.status)}"
        )
    return ProbeResult(
        True,
        f"connected to {endpoint} — token accepted, {int(totals.get('assets', 0) or 0)} assets stored",
        int(totals.get("assets", 0) or 0),
        int(totals.get("totalBytes", 0) or 0),
    )


def delete_asset(
    endpoint: str,
    token: str,
    asset_id: str,
    *,
    timeout: float = DEFAULT_TIMEOUT_S,
    insecure_tls: bool = False,
) -> bool:
    """Delete a row (and its blob, once nothing else references it).

    Returns False for an already-gone row — a GC pass must never be fatal.
    """
    if not asset_id:
        return False
    resp = request(
        "DELETE",
        f"{endpoint.rstrip('/')}/{asset_id}",
        token=token,
        timeout=timeout,
        insecure_tls=insecure_tls,
    )
    if resp.status in (204, 200):
        return True
    if resp.status == 404:
        return False
    raise TransportError(f"delete of {asset_id} failed: {resp.error}", status=resp.status)
