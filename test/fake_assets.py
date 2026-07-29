# SPDX-FileCopyrightText: 2026 caderuntime <caderuntime@cade.run>
# SPDX-License-Identifier: GPL-3.0-or-later

"""fake_assets.py — a stdlib fake of the slice of the assets HTTP contract the addon uses.

The addon talks to a real catalog service over HTTP. That service is a separate
product and is not something a contributor here can be expected to run, so the
live tests would arrive unrunnable on a clean clone. This is the answer: the same
HTTP surface, in the standard library, needing nothing installed.

**A fake cannot tell you it is right**, and that limit is worth stating plainly
rather than papering over: it proves the addon agrees with an implementation
written from the same reading of the contract as the client. The catalog's own
repository runs this same contract body against the real service, which is what
catches the two drifting apart.

So the published contract is the authority, not this file:

    https://cade.run/api/assets-http/

If you change a behaviour here, you are asserting something about that contract —
check it there first.

Scope is deliberately the addon's slice and nothing else: upload, the dedupe
handshake, list-by-tag, blob HEAD, the credential probe, delete. The real service
does much more; implementing any of it here would be inventing a contract nothing
checks.

Where a behaviour below is subtle, the comment says why it is that way, so a
divergence can be traced to a decision rather than re-litigated.
"""

from __future__ import annotations

import hashlib
import json
import re
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

# Make `showcade_bridge` importable when a test file is run DIRECTLY
# (`python3 test/test_policy.py`) as well as under the discovery run, which
# supplies `PYTHONPATH=.` itself. Every test module imports this one for the
# fixtures below, so this is the single place that has to know the layout.
_REPO_ROOT = str(Path(__file__).resolve().parents[1])
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

BASE_PATH = "/api/assets"

#: Mirrors `MAX_MODEL_BYTES` in the addon and the service's own model cap.
MAX_MODEL_BYTES = 32 * 1024 * 1024

#: The service classifies content by magic bytes, not by file extension.
GLB_MAGIC = b"glTF"
GLB_MIME = "model/gltf-binary"

_BLENDER_TAG = re.compile(r"^blender:([0-9A-Za-z_-]{1,64})$")

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


# ── fixtures shared by BOTH backends ─────────────────────────────────────
# Every test module imports this one, so these live here rather than in a
# separate helper: one import gets a test file both the fixtures and the sys.path
# bootstrap above.


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def make_glb(payload: str = "showcade") -> bytes:
    """A minimal, structurally valid GLB.

    The service classifies by the `glTF` magic, so this is all either server
    needs — and varying `payload` varies the sha256, which is
    what the re-send and generation tests turn on.
    """
    json_chunk = (
        '{"asset":{"version":"2.0","generator":"showcade-bridge-test"},'
        f'"scenes":[{{"name":"{payload}"}}],"scene":0}}'
    ).encode()
    json_chunk += b" " * (-len(json_chunk) % 4)  # chunks are 4-byte aligned
    body = len(json_chunk).to_bytes(4, "little") + b"JSON" + json_chunk
    total = 12 + len(body)
    return b"glTF" + (2).to_bytes(4, "little") + total.to_bytes(4, "little") + body


def _ulid(counter: int) -> str:
    """A 26-char Crockford-base32 id that sorts newest-last, like a real ULID.

    The catalog resolves a link with `ORDER BY id DESC` over immutable ULIDs, so
    the ONLY property that matters here is that later ids compare greater as
    strings. A monotonic counter gives that without a clock — and without a
    clock it cannot produce two equal ids inside the same millisecond, which a
    naive time-based generator would do under a fast test loop and would make
    the ordering (and therefore the revert test) intermittently wrong.
    """
    out = []
    n = counter
    for _ in range(26):
        out.append(_CROCKFORD[n % 32])
        n //= 32
    return "".join(reversed(out))


def merge_tags(existing: list[str], incoming: list[str]) -> list[str] | None:
    """Union, keeping `existing` order; `None` when nothing is new.

    `None` is what keeps the dedupe path write-free, so a converged re-send
    touches no row (handler.ts `mergeTags`).
    """
    added = [t for i, t in enumerate(incoming) if incoming.index(t) == i and t not in existing]
    return [*existing, *added] if added else None


class _Catalog:
    """Rows + blobs, in memory. One lock: `ThreadingHTTPServer` serves concurrently."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.rows: list[dict[str, Any]] = []
        self.blobs: dict[str, bytes] = {}
        self._counter = 0

    def next_id(self) -> str:
        self._counter += 1
        return _ulid(self._counter)

    def live(self) -> list[dict[str, Any]]:
        return [r for r in self.rows if not r["deleted"]]

    def find_by_sha_and_name(self, sha: str, name: str) -> dict[str, Any] | None:
        """The dedupe key is `(sha256, name)` — NOT sha alone (handler.ts).

        Returns the NEWEST match, mirroring `SQL_FIND_BY_SHA_AND_NAME`'s
        `ORDER BY id DESC`. This is not a detail: a revert mints a SECOND
        generation carrying identical bytes under the same name (bd
        showcade-natd), so from then on two rows match. Answering with the
        oldest makes the stale-generation check below fire forever — the newer
        generation always outranks it — and every identical re-send mints
        another row instead of converging on a write-free 200.

        Caught by `test_a_revert_re_takes_the_newest_generation`'s final step
        rather than reasoned about: this fake returned the oldest match at first
        and that test failed 201 != 200.
        """
        matches = [r for r in self.live() if r["sha256"] == sha and r["name"] == name]
        return max(matches, key=lambda r: r["id"]) if matches else None

    def list_assets(self, *, kind: str | None, tag: str | None, limit: int) -> list[dict[str, Any]]:
        items = self.live()
        if kind is not None:
            items = [r for r in items if r["kind"] == kind]
        if tag is not None:
            items = [r for r in items if tag in r["tags"]]
        items.sort(key=lambda r: r["id"], reverse=True)  # newest first
        return items[:limit]

    def count_live_refs(self, sha: str) -> int:
        return sum(1 for r in self.live() if r["sha256"] == sha)

    def add(self, *, name: str, kind: str, sha: str, size: int, mime: str, tags: list[str]) -> dict[str, Any]:
        row = {
            "id": self.next_id(),
            "name": name,
            "kind": kind,
            "mime": mime,
            "sha256": sha,
            "size": size,
            "tags": list(tags),
            "createdAt": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
            "deleted": False,
        }
        self.rows.append(row)
        return row

    def stats(self) -> dict[str, int]:
        live = self.live()
        # Distinct blobs, not the sum of row sizes: two rows sharing a blob store
        # its bytes once, which is what makes the delete-time reclaim observable.
        shas = {r["sha256"] for r in live}
        return {"assets": len(live), "totalBytes": sum(len(self.blobs.get(s, b"")) for s in shas)}


def to_meta(row: dict[str, Any]) -> dict[str, Any]:
    """`handler.ts toMeta`: the row minus the fields the wire never carries."""
    return {k: v for k, v in row.items() if k != "deleted"}


def parse_multipart(body: bytes, content_type: str) -> tuple[dict[str, str], dict[str, bytes]]:
    """Split a multipart body into (string fields, file parts).

    **The classification rule is the whole point of this function**: a part with
    a `filename` is a file, one without is a string. That is the property the
    real-service test exists to pin (Bun's `FormData` does exactly this), and
    restating it here is what lets the same tests run against both.
    """
    match = re.search(r'boundary="?([^";]+)"?', content_type)
    if not match:
        raise ValueError("no boundary in Content-Type")
    sep = b"--" + match.group(1).encode()

    fields: dict[str, str] = {}
    files: dict[str, bytes] = {}
    for chunk in body.split(sep):
        if chunk in (b"", b"--\r\n", b"--") or b"Content-Disposition" not in chunk:
            continue
        head, _, payload = chunk.partition(b"\r\n\r\n")
        if not _:
            continue
        payload = payload[:-2] if payload.endswith(b"\r\n") else payload
        headers = head.decode("utf-8", "replace")
        name_m = re.search(r'name="([^"]*)"', headers)
        if not name_m:
            continue
        name = name_m.group(1)
        if re.search(r'filename="([^"]*)"', headers):
            files[name] = payload
        else:
            fields[name] = payload.decode("utf-8", "replace")
    return fields, files


class _Handler(BaseHTTPRequestHandler):
    server_version = "FakeAssets/1.0"

    # ── plumbing ─────────────────────────────────────────────────────────

    def log_message(self, *_args) -> None:
        pass

    @property
    def catalog(self) -> _Catalog:
        return self.server.catalog  # type: ignore[attr-defined]

    @property
    def token(self) -> str:
        return self.server.token  # type: ignore[attr-defined]

    def _send(self, status: int, payload: dict[str, Any] | None = None) -> None:
        body = b"" if payload is None else json.dumps(payload).encode()
        self.send_response(status)
        if payload is not None:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body and self.command != "HEAD":
            self.wfile.write(body)

    def _error(self, status: int, message: str) -> None:
        self._send(status, {"error": message})

    def _authorized(self) -> bool:
        header = self.headers.get("Authorization", "")
        return header == f"Bearer {self.token}"

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def _route(self) -> tuple[str, list[str]]:
        """(kind, params) for the addon's slice; kind is '' when nothing matches."""
        path = urlparse(self.path).path
        if path == f"{BASE_PATH}/stats":
            return "stats", []
        if path == BASE_PATH:
            return "collection", []
        blob = re.fullmatch(rf"{re.escape(BASE_PATH)}/blob/([0-9a-f]{{64}})", path)
        if blob:
            return "blob", [blob.group(1)]
        item = re.fullmatch(rf"{re.escape(BASE_PATH)}/([0-9A-Za-z_-]{{1,64}})", path)
        if item:
            return "item", [item.group(1)]
        return "", []

    # ── methods ──────────────────────────────────────────────────────────

    def do_GET(self) -> None:
        kind, params = self._route()
        if kind == "stats":
            with self.catalog.lock:
                self._send(200, {"v": 1, **self.catalog.stats()})
            return
        if kind == "collection":
            query = parse_qs(urlparse(self.path).query)
            with self.catalog.lock:
                items = self.catalog.list_assets(
                    kind=(query.get("kind") or [None])[0],
                    tag=(query.get("tag") or [None])[0],
                    limit=int((query.get("limit") or ["50"])[0]),
                )
                self._send(200, {"items": [to_meta(r) for r in items]})
            return
        if kind == "blob":
            with self.catalog.lock:
                present = params[0] in self.catalog.blobs
            self._send(200 if present else 404, {} if present else {"error": "unknown blob"})
            return
        self._error(404, "not found")

    def do_HEAD(self) -> None:
        # Unauthenticated by design — step 1 of the dedupe handshake.
        kind, params = self._route()
        if kind != "blob":
            self._send(404)
            return
        with self.catalog.lock:
            present = params[0] in self.catalog.blobs
        self._send(200 if present else 404)

    def do_PATCH(self) -> None:
        # AUTHORIZE BEFORE THE LOOKUP. This ordering is the entire reason the
        # addon can probe a token without writing anything: 401 = bad token,
        # 404 = good token and no such asset (handler.ts handlePatch).
        self._read_body()
        if not self._authorized():
            self._error(401, "bad token")
            return
        kind, params = self._route()
        if kind != "item":
            self._error(404, "not found")
            return
        with self.catalog.lock:
            row = next((r for r in self.catalog.live() if r["id"] == params[0]), None)
        self._error(404, "unknown asset") if row is None else self._send(200, to_meta(row))

    def do_DELETE(self) -> None:
        if not self._authorized():
            self._error(401, "bad token")
            return
        kind, params = self._route()
        if kind != "item":
            self._error(404, "not found")
            return
        with self.catalog.lock:
            row = next((r for r in self.catalog.live() if r["id"] == params[0]), None)
            if row is None:
                self._error(404, "unknown asset")
                return
            row["deleted"] = True
            # Delete-time blob GC: reclaim only once NO live row references the
            # blob, so deleting one of two rows sharing bytes keeps them.
            if self.catalog.count_live_refs(row["sha256"]) == 0:
                self.catalog.blobs.pop(row["sha256"], None)
        self._send(204)

    def do_POST(self) -> None:
        body = self._read_body()
        if not self._authorized():
            self._error(401, "bad token")
            return
        kind, _ = self._route()
        if kind != "collection":
            self._error(404, "not found")
            return
        try:
            fields, files = parse_multipart(body, self.headers.get("Content-Type", ""))
        except ValueError as exc:
            self._error(400, str(exc))
            return
        try:
            meta = json.loads(fields.get("meta", ""))
        except json.JSONDecodeError:
            self._error(400, "meta is not JSON")
            return

        name, sha = meta.get("name"), meta.get("sha256")
        row_kind = meta.get("kind", "model")
        tags = list(meta.get("tags") or [])
        if not name or not sha:
            self._error(400, "meta needs name and sha256")
            return

        blob = files.get("file")
        if blob is not None:
            if len(blob) > MAX_MODEL_BYTES:
                self._error(413, "over the 32 MB model cap")
                return
            if hashlib.sha256(blob).hexdigest() != sha:
                self._error(400, "sha256 does not match the bytes")
                return

        with self.catalog.lock:
            if blob is None and sha not in self.catalog.blobs:
                # Metadata-only for a blob the catalog has never seen. The addon
                # heals this by re-sending with the bytes (bd showcade-u4nw).
                self._error(404, "unknown blob — resend with the bytes")
                return
            if blob is not None:
                self.catalog.blobs[sha] = blob
            size = len(self.catalog.blobs[sha])
            mime = GLB_MIME if self.catalog.blobs[sha].startswith(GLB_MAGIC) else "application/octet-stream"

            existing = self.catalog.find_by_sha_and_name(sha, name)
            if existing is None:
                self._send(201, to_meta(self.catalog.add(
                    name=name, kind=row_kind, sha=sha, size=size, mime=mime, tags=tags,
                )))
                return

            merged = merge_tags(existing["tags"], tags)
            if self._is_stale_link_generation(existing, tags):
                # A REVERT (bd showcade-natd): answering with `existing` would leave a
                # NEWER row highest, and the resolver takes the newest — so the link
                # would keep serving the other generation forever. Mint a fresh
                # generation for bytes we already hold. The matched row still takes
                # the merged tags, so it stays linkable too (bd showcade-edhp).
                if merged is not None:
                    existing["tags"] = merged
                self._send(201, to_meta(self.catalog.add(
                    name=name, kind=row_kind, sha=sha, size=size, mime=mime,
                    tags=merged if merged is not None else existing["tags"],
                )))
                return
            if merged is None:
                self._send(200, to_meta(existing))  # converged: write-free
                return
            existing["tags"] = merged
            self._send(200, to_meta(existing))

    def _is_stale_link_generation(self, existing: dict[str, Any], incoming: list[str]) -> bool:
        """Would answering with `existing` resolve the WRONG bytes for a link tag?

        Same slice as the resolver (`kind=model`, the tag, limit 1) — reading
        wider made a CURRENT dedupe look stale and minted a spurious generation
        (bd showcade-14uo). NOT stale has two shapes and both must stay 200: the
        slice is empty (a brand-new tag is borne by nothing), or its top row IS
        `existing` (the steady state — Blender re-sending the current
        generation). Treating "the slice has any row" as stale would mint a
        generation per send (bd showcade-natd).
        """
        for tag in incoming:
            if not _BLENDER_TAG.match(tag):
                continue
            if existing["kind"] != "model":
                continue  # outside the resolved slice ⇒ never stale
            page = self.catalog.list_assets(kind="model", tag=tag, limit=1)
            if page and page[0]["id"] != existing["id"]:
                return True
        return False


class FakeAssetsServer:
    """A stdlib stand-in presenting `AssetsServer`'s interface.

    The interface is `endpoint`, `token`, `start()`, `stop()` — deliberately the
    same shape the catalog repository's real-service harness presents, so one
    contract body can be pointed at either without editing it.
    """

    def __init__(self, token: str = "blender-test-token"):
        self.token = token
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def endpoint(self) -> str:
        if self._server is None:  # pragma: no cover - misuse
            raise RuntimeError("start() first")
        return f"http://127.0.0.1:{self._server.server_address[1]}{BASE_PATH}"

    def start(self) -> FakeAssetsServer:
        server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        server.catalog = _Catalog()  # type: ignore[attr-defined]
        server.token = self.token  # type: ignore[attr-defined]
        self._server = server
        self._thread = threading.Thread(target=server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        """Idempotent, and safe before `start()` — mirrors `AssetsServer.stop`."""
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=10)
            self._thread = None
