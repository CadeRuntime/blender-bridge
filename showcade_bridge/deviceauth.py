# SPDX-FileCopyrightText: 2026 caderuntime <caderuntime@cade.run>
# SPDX-License-Identifier: GPL-3.0-or-later

"""deviceauth.py — sign in to showcade from Blender via the OAuth 2.0 device grant.

**This module imports no `bpy`**, like `transport.py` and for the same reason: the whole
flow is then testable under plain `unittest` without Blender in the loop
(`task test:blender`). Stdlib only — no `requests`.

WHY A DEVICE FLOW AT ALL (bd showcade-fmq8). The static upload token in the addon
preferences only works while a deployment is in the *bearer era*. Once an operator sets
a deployment configured to verify user sessions routes every write
through `authorizeSession` — which wants a Better Auth session, not a shared secret — and the
bearer path is bypassed entirely, not used as a fallback. Blender has no browser and no
cookie jar, so it has nothing to send.

RFC 8628 solves exactly this shape: the addon asks for a short user code, the human approves
it in a REAL browser (where the Google/Discord/Twitch sign-in already works), and the addon
polls until a session token comes back. No loopback listener, no embedded browser, and the
addon never sees a password or an OAuth client secret.

The endpoint shapes below were read off a running `task dev:auth`, not off the plugin's
types: `POST /device/code` → `{device_code, user_code, verification_uri,
verification_uri_complete, expires_in, interval}`; `POST /device/token` → an
`{access_token, ...}` on success or `{error, error_description}` while waiting.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any, NamedTuple

from . import transport

__all__ = [
    "CLIENT_ID",
    "DEFAULT_AUTH_ENDPOINT",
    "DeviceAuthError",
    "DeviceCode",
    "poll_for_token",
    "request_device_code",
]

#: Must match services/auth/src/deviceClients.ts BLENDER_ADDON_CLIENT_ID exactly. The
#: device endpoints run a CLOSED allow-list (`validateClient`), so a mismatch is an
#: `invalid_client` 400 and nothing else — there is no partial-credit failure to debug from.
CLIENT_ID = "showcade-blender"

#: `task dev:auth` serves the auth Worker here. Production is https://show.cade.run/api/auth.
DEFAULT_AUTH_ENDPOINT = "http://localhost:8787/api/auth"

#: RFC 8628 §3.5 — the two errors that mean "keep waiting", vs everything else which is
#: terminal. `slow_down` ALSO mandates lengthening the interval, which is the part that gets
#: skipped: a client that treats it as a synonym for `authorization_pending` keeps hammering
#: at the old rate and stays throttled.
_PENDING = "authorization_pending"
_SLOW_DOWN = "slow_down"

#: How much to add to the poll interval on each `slow_down`, per RFC 8628 §3.5.
_SLOW_DOWN_STEP_S = 5.0


class DeviceAuthError(RuntimeError):
    """A device sign-in that cannot succeed, with an operator-facing reason."""

    def __init__(self, message: str, *, code: str | None = None):
        super().__init__(message)
        self.code = code


class DeviceCode(NamedTuple):
    """The pending authorization: what to show the user, and what to poll with."""

    device_code: str
    """Opaque; polled with, never shown — it is the bearer of the pending grant."""
    user_code: str
    """Short code the HUMAN types (or confirms) in the browser."""
    verification_uri: str
    """Where to approve. Defaults to `<auth baseURL>/device`."""
    verification_uri_complete: str
    """Same page with `?user_code=` prefilled — prefer it when opening a browser."""
    expires_in: int
    """Seconds until the code dies; the poll loop refuses to outlive it."""
    interval: float
    """Server-chosen seconds between polls. Honour it — do not pick your own."""

    @property
    def best_uri(self) -> str:
        """The URI to hand a human: prefilled when the server offered one."""
        return self.verification_uri_complete or self.verification_uri


def _endpoint(auth_endpoint: str, path: str) -> str:
    # rstrip mirrors every transport call site: a configured endpoint with a trailing slash
    # would otherwise produce a double slash and a 404 that reads like a wrong host.
    return f"{auth_endpoint.rstrip('/')}/{path}"


def _auth_request(*args: Any, **kwargs: Any) -> transport.Response:
    """`transport.request`, but failures blame the AUTH service.

    transport.py is the CATALOG client, so its TransportError defaults to a catalog hint —
    "could not reach the catalog — is `task dev:assets` running on that endpoint?". Reusing
    the request plumbing for auth calls (which is right — one HTTP layer, stdlib only) inherits
    that wording, so a sign-in against a down auth service sent the operator to check the
    wrong service entirely. Found while walking the flow by hand.
    """
    try:
        return transport.request(*args, **kwargs)
    except transport.TransportError as exc:
        raise DeviceAuthError(
            f"could not reach the sign-in service — is it running on that endpoint? ({exc.args[0]})"
        ) from exc


def request_device_code(
    auth_endpoint: str = DEFAULT_AUTH_ENDPOINT,
    *,
    scope: str | None = None,
    timeout: float = transport.DEFAULT_TIMEOUT_S,
    insecure_tls: bool = False,
) -> DeviceCode:
    """Start a sign-in: ask for a user code the human can approve."""
    payload: dict[str, str] = {"client_id": CLIENT_ID}
    if scope:
        payload["scope"] = scope
    resp = _auth_request(
        "POST",
        _endpoint(auth_endpoint, "device/code"),
        body=json.dumps(payload).encode("utf-8"),
        content_type="application/json",
        timeout=timeout,
        insecure_tls=insecure_tls,
    )
    body = resp.json()
    if resp.status != 200:
        # invalid_client is the one worth naming outright: it means this build's CLIENT_ID is
        # not on the deployment's allow-list, which no amount of retrying fixes.
        code = str(body.get("error") or "")
        detail = str(body.get("error_description") or resp.error)
        if code == "invalid_client":
            raise DeviceAuthError(
                f"this showcade build is not an accepted device client ({CLIENT_ID}) — "
                "the endpoint may predate device sign-in",
                code=code,
            )
        raise DeviceAuthError(f"could not start sign-in: {detail}", code=code or None)

    missing = [k for k in ("device_code", "user_code") if not body.get(k)]
    if missing:
        # A 200 without these is not something to poll on; say so rather than looping on
        # empty strings until the deadline.
        raise DeviceAuthError(f"sign-in response missing {', '.join(missing)}")

    return DeviceCode(
        device_code=str(body["device_code"]),
        user_code=str(body["user_code"]),
        verification_uri=str(body.get("verification_uri") or ""),
        verification_uri_complete=str(body.get("verification_uri_complete") or ""),
        expires_in=int(body.get("expires_in") or 0),
        interval=float(body.get("interval") or 5),
    )


def poll_for_token(
    pending: DeviceCode,
    auth_endpoint: str = DEFAULT_AUTH_ENDPOINT,
    *,
    timeout: float = transport.DEFAULT_TIMEOUT_S,
    insecure_tls: bool = False,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.monotonic,
) -> str:
    """Poll until the human approves, and return the session token.

    Raises `DeviceAuthError` on denial, expiry, or any non-pending error.

    `sleep`/`now` are injected so a test can prove the BACKOFF changed without waiting out
    real seconds — the assertion that matters here is about timing, and a test that could
    only check the return value would pass against a client that ignored `slow_down`.
    """
    interval = pending.interval
    # A deadline of our own, so a server that never says `expired_token` cannot wedge the
    # loop forever. expires_in of 0 (absent) falls back to the RFC's 1800s default shape.
    deadline = now() + (pending.expires_in or 1800)

    while True:
        if now() >= deadline:
            raise DeviceAuthError("sign-in expired before it was approved", code="expired_token")

        resp = _auth_request(
            "POST",
            _endpoint(auth_endpoint, "device/token"),
            body=json.dumps(
                {
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                    "device_code": pending.device_code,
                    "client_id": CLIENT_ID,
                }
            ).encode("utf-8"),
            content_type="application/json",
            timeout=timeout,
            insecure_tls=insecure_tls,
        )
        body = resp.json()
        token = body.get("access_token") or body.get("session_token")
        if resp.status == 200 and token:
            return str(token)

        code = str(body.get("error") or "")
        if code == _SLOW_DOWN:
            # Lengthen, THEN wait. Treating this as a plain retry is the classic RFC 8628
            # bug: the server asked us to back off, so polling at the old rate keeps us
            # throttled and can burn the whole window.
            interval += _SLOW_DOWN_STEP_S
        elif code != _PENDING:
            detail = str(body.get("error_description") or resp.error)
            raise DeviceAuthError(f"sign-in failed: {detail}", code=code or None)

        sleep(interval)
