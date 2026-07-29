# SPDX-FileCopyrightText: 2026 caderuntime <caderuntime@cade.run>
# SPDX-License-Identifier: GPL-3.0-or-later

"""The device authorization grant, against a stub auth service (bd showcade-fmq8).

The RESPONSE SHAPES here were copied from a running `task dev:auth`, not from the plugin's
type declarations — a stub built from my reading of the types would encode my misreading and
then pass against it, which is exactly how a wrong wire format ships. Observed:

    POST /device/code  → {"device_code","user_code","verification_uri",
                          "verification_uri_complete","expires_in":1800,"interval":5}
    POST /device/token → {"error":"authorization_pending","error_description":"..."}
    unknown client_id  → 400 {"error":"invalid_client","error_description":"Invalid client ID"}

What actually needs guarding is the POLL LOOP, because every failure mode there is silent:
a client that ignores `slow_down` stays throttled, one that treats an unknown error as
"pending" spins until the code expires, and one with no deadline of its own hangs forever if
the server never says `expired_token`.
"""

from __future__ import annotations

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from showcade_bridge import deviceauth


class _Stub(BaseHTTPRequestHandler):
    """Replies from a scripted queue; records the client_id each request carried."""

    # Set per-test on the server instance.
    def do_POST(self):
        length = int(self.headers.get("content-length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        self.server.seen.append({"path": self.path, "body": body})
        status, payload = self.server.script.pop(0) if self.server.script else (500, {})
        blob = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(blob)))
        self.end_headers()
        self.wfile.write(blob)

    def log_message(self, *_args):  # keep the test output clean
        return


class _StubServer:
    """A one-thread HTTP stub on an ephemeral port."""

    def __init__(self, script):
        self.httpd = HTTPServer(("127.0.0.1", 0), _Stub)
        self.httpd.script = list(script)
        self.httpd.seen = []
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def endpoint(self) -> str:
        host, port = self.httpd.server_address[:2]
        return f"http://{host}:{port}/api/auth"

    @property
    def seen(self):
        return self.httpd.seen

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)


CODE_OK = (
    200,
    {
        "device_code": "dev-code-1",
        "user_code": "AUYBB5MB",
        "verification_uri": "http://localhost:8787/device",
        "verification_uri_complete": "http://localhost:8787/device?user_code=AUYBB5MB",
        "expires_in": 1800,
        "interval": 5,
    },
)


def pending():
    return (400, {"error": "authorization_pending", "error_description": "Authorization pending"})


def slow_down():
    return (400, {"error": "slow_down", "error_description": "Slow down"})


def granted(token="sess-tok"):
    return (200, {"access_token": token, "token_type": "Bearer"})


class RequestDeviceCode(unittest.TestCase):
    def test_parses_the_pending_authorization(self):
        srv = _StubServer([CODE_OK])
        try:
            pend = deviceauth.request_device_code(srv.endpoint)
        finally:
            srv.stop()
        self.assertEqual(pend.user_code, "AUYBB5MB")
        self.assertEqual(pend.interval, 5)
        self.assertEqual(pend.expires_in, 1800)
        # The prefilled URI is what a human should be sent to when the server offers one.
        self.assertEqual(pend.best_uri, "http://localhost:8787/device?user_code=AUYBB5MB")

    def test_sends_the_allow_listed_client_id(self):
        # The deployment runs a CLOSED allow-list (services/auth deviceClients.ts), so this
        # literal is load-bearing: a mismatch is an invalid_client 400 and nothing else.
        srv = _StubServer([CODE_OK])
        try:
            deviceauth.request_device_code(srv.endpoint)
        finally:
            srv.stop()
        self.assertEqual(srv.seen[0]["body"]["client_id"], "showcade-blender")
        self.assertTrue(srv.seen[0]["path"].endswith("/device/code"))

    def test_invalid_client_says_the_build_is_not_accepted(self):
        srv = _StubServer([(400, {"error": "invalid_client", "error_description": "Invalid client ID"})])
        try:
            with self.assertRaises(deviceauth.DeviceAuthError) as caught:
                deviceauth.request_device_code(srv.endpoint)
        finally:
            srv.stop()
        self.assertEqual(caught.exception.code, "invalid_client")

    def test_a_200_missing_the_codes_is_refused_not_polled(self):
        # Polling on an empty device_code would spin to the deadline reporting nothing.
        srv = _StubServer([(200, {"interval": 5})])
        try:
            with self.assertRaises(deviceauth.DeviceAuthError) as caught:
                deviceauth.request_device_code(srv.endpoint)
        finally:
            srv.stop()
        self.assertIn("device_code", str(caught.exception))


class UnreachableServiceBlamesAuth(unittest.TestCase):
    """A down auth service must not send the operator to the CATALOG (bd showcade-fmq8).

    transport.py is the catalog client, so its TransportError hint says "is `task dev:assets`
    running". deviceauth reuses that request plumbing — correctly, one HTTP layer — and
    inherited the wrong advice: a failed sign-in told you to check the asset service. Found by
    walking the flow by hand, not by any test that existed.
    """

    #: Nothing listens here. Port 1 is privileged and unbindable, so this cannot flake by
    #: accidentally hitting a real service.
    DEAD = "http://127.0.0.1:1/api/auth"

    def test_request_device_code_blames_the_sign_in_service(self):
        with self.assertRaises(deviceauth.DeviceAuthError) as caught:
            deviceauth.request_device_code(self.DEAD, timeout=2.0)
        message = str(caught.exception)
        self.assertIn("sign-in service", message)
        self.assertNotIn("dev:assets", message)
        self.assertNotIn("catalog", message)

    def test_poll_blames_the_sign_in_service_too(self):
        pending = deviceauth.DeviceCode(
            device_code="d", user_code="U", verification_uri="", verification_uri_complete="",
            expires_in=1800, interval=1,
        )
        with self.assertRaises(deviceauth.DeviceAuthError) as caught:
            deviceauth.poll_for_token(pending, self.DEAD, timeout=2.0, sleep=lambda _s: None)
        self.assertNotIn("dev:assets", str(caught.exception))


class PollForToken(unittest.TestCase):
    def _pending(self, **kw):
        return deviceauth.DeviceCode(
            device_code="dev-code-1",
            user_code="AUYBB5MB",
            verification_uri="http://x/device",
            verification_uri_complete="",
            expires_in=kw.get("expires_in", 1800),
            interval=kw.get("interval", 5),
        )

    def test_waits_through_pending_then_returns_the_token(self):
        srv = _StubServer([pending(), pending(), granted("sess-abc")])
        slept = []
        try:
            token = deviceauth.poll_for_token(
                self._pending(), srv.endpoint, sleep=slept.append, now=lambda: 0.0
            )
        finally:
            srv.stop()
        self.assertEqual(token, "sess-abc")
        # Two waits for two pendings, both at the SERVER's interval — not one we invented.
        self.assertEqual(slept, [5, 5])

    def test_slow_down_LENGTHENS_the_interval(self):
        # The assertion that matters. A client treating slow_down as a synonym for
        # authorization_pending returns the same token and passes any test that only checks
        # the return value — while staying throttled against a real server.
        srv = _StubServer([slow_down(), pending(), granted()])
        slept = []
        try:
            deviceauth.poll_for_token(
                self._pending(interval=5), srv.endpoint, sleep=slept.append, now=lambda: 0.0
            )
        finally:
            srv.stop()
        self.assertEqual(slept[0], 10, "slow_down must add to the interval")
        self.assertEqual(slept[1], 10, "the lengthened interval must PERSIST, not snap back")

    def test_access_denied_is_terminal(self):
        srv = _StubServer([(400, {"error": "access_denied", "error_description": "Denied"})])
        try:
            with self.assertRaises(deviceauth.DeviceAuthError) as caught:
                deviceauth.poll_for_token(self._pending(), srv.endpoint, sleep=lambda _s: None)
        finally:
            srv.stop()
        self.assertEqual(caught.exception.code, "access_denied")

    def test_an_unknown_error_is_terminal_not_treated_as_pending(self):
        # Spinning on an unrecognised error is how a client burns the whole window and then
        # reports "expired" for a problem the server named on the first poll.
        srv = _StubServer([(400, {"error": "server_borked", "error_description": "Boom"})])
        try:
            with self.assertRaises(deviceauth.DeviceAuthError) as caught:
                deviceauth.poll_for_token(self._pending(), srv.endpoint, sleep=lambda _s: None)
        finally:
            srv.stop()
        self.assertEqual(caught.exception.code, "server_borked")

    def test_its_own_deadline_stops_a_server_that_never_expires_the_code(self):
        # expires_in is 1 second and the clock jumps past it: the loop must give up on its
        # OWN deadline rather than trusting the server to ever say expired_token.
        srv = _StubServer([pending()] * 20)
        clock = iter([0.0, 5.0, 100.0])
        try:
            with self.assertRaises(deviceauth.DeviceAuthError) as caught:
                deviceauth.poll_for_token(
                    self._pending(expires_in=1),
                    srv.endpoint,
                    sleep=lambda _s: None,
                    now=lambda: next(clock),
                )
        finally:
            srv.stop()
        self.assertEqual(caught.exception.code, "expired_token")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
