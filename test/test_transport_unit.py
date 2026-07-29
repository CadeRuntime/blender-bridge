# SPDX-FileCopyrightText: 2026 caderuntime <caderuntime@cade.run>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Pure-unit tests for the multipart encoder and the identity helpers.

No Blender, no server, no network — these assert the *invariants* the service's
part classification depends on (a `meta` part must have no filename; a `file`
part must have one) plus the tag-length arithmetic that a UUID would have broken.
"""

from __future__ import annotations

import hashlib
import json
import unittest
import unittest.mock
from email.parser import BytesParser
from email.policy import HTTP

import fake_assets

from showcade_bridge import transport


def parse_parts(content_type: str, body: bytes):
    """Decode a multipart body the way an HTTP server would."""
    message = BytesParser(policy=HTTP).parsebytes(
        f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode() + body
    )
    assert message.is_multipart(), "encoder produced a non-multipart body"
    return list(message.iter_parts())


class MultipartEncoding(unittest.TestCase):
    def test_meta_part_has_no_filename_and_no_content_type(self):
        # This is the whole reason the encoder is hand-rolled: a filename-less
        # part decodes to a STRING, which is what the service demands of `meta`.
        content_type, body = transport.encode_multipart({"meta": '{"name":"x"}'})
        parts = parse_parts(content_type, body)
        self.assertEqual(len(parts), 1)
        disposition = parts[0]["Content-Disposition"]
        self.assertIn('name="meta"', disposition)
        self.assertNotIn("filename", disposition)
        self.assertIsNone(parts[0].get("Content-Type"))

    def test_file_part_carries_filename_and_mime(self):
        content_type, body = transport.encode_multipart(
            {"meta": "{}"},
            [transport.FilePart("file", "Bumper.glb", "model/gltf-binary", b"glTFbytes")],
        )
        parts = parse_parts(content_type, body)
        self.assertEqual(len(parts), 2)
        file_part = parts[1]
        self.assertIn('filename="Bumper.glb"', file_part["Content-Disposition"])
        self.assertEqual(file_part.get_content_type(), "model/gltf-binary")
        self.assertEqual(file_part.get_payload(decode=True), b"glTFbytes")

    def test_binary_payload_survives_verbatim(self):
        # Every byte value, including CRLF runs and the boundary-ish "--".
        blob = bytes(range(256)) + b"\r\n--nope\r\n" + bytes(range(256))
        content_type, body = transport.encode_multipart(
            {"meta": "{}"}, [transport.FilePart("file", "a.glb", "model/gltf-binary", blob)]
        )
        parts = parse_parts(content_type, body)
        self.assertEqual(parts[1].get_payload(decode=True), blob)

    def test_a_hostile_filename_cannot_forge_a_part(self):
        hostile = 'evil"\r\n--x\r\nContent-Disposition: form-data; name="thumb"\r\n\r\npwned'
        content_type, body = transport.encode_multipart(
            {"meta": "{}"}, [transport.FilePart("file", hostile, "model/gltf-binary", b"ok")]
        )
        parts = parse_parts(content_type, body)
        # The quote and the CRLFs are neutered, so the payload stays one part
        # named "file" — the injected "thumb" part never materializes.
        self.assertEqual(len(parts), 2, "header injection created an extra part")
        self.assertEqual([p.get_param("name", header="Content-Disposition") for p in parts], ["meta", "file"])
        self.assertEqual(parts[1].get_payload(decode=True), b"ok")

    def test_body_is_deterministic_for_a_fixed_boundary(self):
        args = ({"meta": "{}"}, [transport.FilePart("file", "a.glb", "model/gltf-binary", b"xy")])
        first = transport.encode_multipart(*args, boundary="fixedboundary")
        second = transport.encode_multipart(*args, boundary="fixedboundary")
        self.assertEqual(first, second)
        self.assertTrue(first[1].endswith(b"--fixedboundary--\r\n"))

    def test_generated_boundaries_differ(self):
        boundaries = {
            transport.encode_multipart({"meta": "{}"})[0] for _ in range(32)
        }
        self.assertEqual(len(boundaries), 32)

    def test_boundary_colliding_with_the_payload_is_refused(self):
        with self.assertRaises(ValueError):
            transport.encode_multipart(
                {"meta": "{}"},
                [transport.FilePart("file", "a.glb", "x/y", b"..collide..")],
                boundary="collide",
            )


class Identity(unittest.TestCase):
    def test_link_tag_fits_the_server_cap(self):
        # 'blender:' + a 36-char uuid is 44 chars and would 400 against
        # MAX_TAG_LEN=40; 8 random bytes hex keeps it at 24.
        tag = transport.link_tag(transport.new_link_id())
        self.assertTrue(tag.startswith(transport.LINK_TAG_PREFIX))
        self.assertEqual(len(tag), 24)
        self.assertLessEqual(len(tag), transport.MAX_TAG_LEN)

    def test_link_tag_is_a_pure_function_of_the_id(self):
        link_id = transport.new_link_id()
        self.assertEqual(transport.link_tag(link_id), transport.link_tag(link_id))

    def test_new_link_ids_are_unique(self):
        self.assertEqual(len({transport.new_link_id() for _ in range(512)}), 512)

    def test_malformed_link_ids_are_refused(self):
        for bad in ("", "nothex!", "ABCDEF0123456789", "0123", "0" * 40):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                transport.link_tag(bad)

    def test_model_tags_always_record_provenance_and_units(self):
        tags = transport.model_tags()
        self.assertIn("blender", tags)
        # The addon exports true metres and never bakes a scale — the tag is the
        # contract that says so.
        self.assertIn("units:m", tags)

    def test_model_tags_are_idempotent_and_dedupe_extras(self):
        link_id = transport.new_link_id()
        first = transport.model_tags(link_id, ["blender", "prop"])
        second = transport.model_tags(link_id, ["blender", "prop"])
        self.assertEqual(first, second)
        self.assertEqual(first.count("blender"), 1)
        self.assertIn(transport.link_tag(link_id), first)

    def test_model_tags_respect_the_server_caps(self):
        with self.assertRaises(ValueError):
            transport.model_tags(None, [f"t{i}" for i in range(transport.MAX_TAGS)])
        with self.assertRaises(ValueError):
            transport.model_tags(None, ["x" * (transport.MAX_TAG_LEN + 1)])

    def test_sha256_matches_hashlib(self):
        data = b"glTF-ish"
        self.assertEqual(transport.sha256_hex(data), hashlib.sha256(data).hexdigest())


class LocalValidation(unittest.TestCase):
    """Bad input must fail before a socket is opened — the endpoint here is
    deliberately unroutable, so any network attempt would raise TransportError
    instead of ValueError."""

    DEAD = "http://127.0.0.1:1/api/assets"

    def test_empty_payload_is_refused_locally(self):
        with self.assertRaises(ValueError):
            transport.upload_model(self.DEAD, "t", name="x", data=b"")

    def test_overlong_name_is_refused_locally(self):
        with self.assertRaises(ValueError):
            transport.upload_model(self.DEAD, "t", name="n" * 121, data=b"glTF")

    def test_blank_name_is_refused_locally(self):
        with self.assertRaises(ValueError):
            transport.upload_model(self.DEAD, "t", name="   ", data=b"glTF")

    def test_an_oversize_glb_never_reaches_the_wire(self):
        # bd showcade-81il: MAX_MODEL_BYTES was mirrored from the service but
        # never checked, so 32 MB+ was hashed, buffered and uploaded in full
        # before the 413. The unroutable endpoint is the proof: a request would
        # raise TransportError, so a ValueError means nothing was sent.
        oversize = b"glTF" + b"\0" * transport.MAX_MODEL_BYTES
        with self.assertRaises(ValueError) as caught:
            transport.upload_model(self.DEAD, "t", name="huge", data=oversize)
        self.assertIn("32 MB cap", str(caught.exception))

    def test_a_glb_exactly_at_the_cap_is_allowed_through(self):
        # The guard is >, not >=: the service accepts the cap itself, so the
        # addon must not refuse a file the catalog would have taken.
        at_cap = b"\0" * transport.MAX_MODEL_BYTES
        with self.assertRaises(transport.TransportError):  # got to the socket
            transport.upload_model(self.DEAD, "t", name="at-cap", data=at_cap, timeout=2.0)

    def test_delete_of_no_id_is_a_no_op(self):
        self.assertFalse(transport.delete_asset(self.DEAD, "t", ""))

    def test_unreachable_endpoint_names_the_fix(self):
        with self.assertRaises(transport.TransportError) as caught:
            transport.blob_exists(self.DEAD, "0" * 64, timeout=2.0)
        self.assertIn("dev:assets", caught.exception.hint)
        self.assertIsNone(caught.exception.status)


class MetadataOnlyFallback(unittest.TestCase):
    """bd showcade-u4nw — the blob store and the catalog can disagree.

    `blob_exists` probes the BLOB STORE; a metadata-only POST needs the
    CATALOG's blob row. A crash between `blobs.put` and `ensureBlob`, or a
    delete racing our HEAD, leaves the first true and the second missing — the
    server then answers 404 and `hint_for(404)` blames the endpoint, which is
    both wrong and unactionable. We are still holding the bytes, so send them.
    """

    ENDPOINT = "http://catalog.invalid/api/assets"

    def _fake_request(self, responses):
        """Serve `responses` in order, recording every call."""
        calls = []

        def fake(method, url, *, body=None, content_type=None, **kw):
            calls.append({"method": method, "url": url, "body": body or b""})
            return responses[len(calls) - 1]

        return fake, calls

    def test_a_404_on_the_metadata_only_post_retries_WITH_the_bytes(self):
        data = b"glTF-body-bytes"
        fake, calls = self._fake_request(
            [
                transport.Response(200, b""),  # HEAD: the blob store has it
                transport.Response(404, b'{"error":"unknown blob"}'),  # catalog disagrees
                transport.Response(201, b'{"id":"AS1","name":"x","kind":"model"}'),
            ]
        )
        with unittest.mock.patch.object(transport, "request", fake):
            result = transport.upload_model(self.ENDPOINT, "t", name="x", data=data)

        self.assertEqual(len(calls), 3, "HEAD, metadata-only POST, then the retry")
        self.assertNotIn(data, calls[1]["body"], "the first POST is metadata-only")
        self.assertIn(data, calls[2]["body"], "the retry carries the bytes")
        self.assertEqual(result.status, 201)
        self.assertEqual(result.sent_bytes, len(data), "the bytes DID cross the wire")

    def test_a_404_that_already_carried_the_bytes_is_not_retried(self):
        fake, calls = self._fake_request(
            [
                transport.Response(404, b""),  # HEAD: blob store has never seen it
                transport.Response(404, b'{"error":"no route"}'),  # genuinely unroutable
            ]
        )
        with (
            unittest.mock.patch.object(transport, "request", fake),
            self.assertRaises(transport.TransportError) as caught,
        ):
            transport.upload_model(self.ENDPOINT, "t", name="x", data=b"glTF")

        self.assertEqual(len(calls), 2, "no third request — one retry, not a loop")
        self.assertEqual(caught.exception.status, 404)

    def test_a_non_404_failure_is_not_retried_either(self):
        fake, calls = self._fake_request(
            [transport.Response(200, b""), transport.Response(401, b'{"error":"bad token"}')]
        )
        with (
            unittest.mock.patch.object(transport, "request", fake),
            self.assertRaises(transport.TransportError) as caught,
        ):
            transport.upload_model(self.ENDPOINT, "t", name="x", data=b"glTF")

        self.assertEqual(len(calls), 2)
        self.assertEqual(caught.exception.status, 401)


class Hints(unittest.TestCase):
    def test_401_and_403_name_different_actions(self):
        # 403 means "read-only endpoint, you are on prod"; 401 means "bad token".
        self.assertNotEqual(transport.hint_for(401), transport.hint_for(403))
        self.assertIn("token", transport.hint_for(401))
        self.assertIn("read-only", transport.hint_for(403))

    def test_413_names_the_cap(self):
        self.assertIn("32 MB", transport.hint_for(413))


class MetaShape(unittest.TestCase):
    """The `meta` JSON the server parses — asserted through the encoder so a
    change to either side shows up here."""

    def test_meta_declares_kind_model_and_the_sha(self):
        data = fake_assets.make_glb("meta-shape")
        meta = {
            "name": "Bumper",
            "kind": "model",
            "sha256": transport.sha256_hex(data),
            "tags": transport.model_tags(),
        }
        content_type, body = transport.encode_multipart({"meta": json.dumps(meta)})
        decoded = json.loads(parse_parts(content_type, body)[0].get_payload())
        self.assertEqual(decoded["kind"], "model")
        self.assertEqual(decoded["sha256"], hashlib.sha256(data).hexdigest())



class CredentialPrecedence(unittest.TestCase):
    """Which of the two credentials a write carries (bd showcade-fmq8).

    Two coexist because two eras do: a session-era deployment verifies a Better Auth
    session, a bearer-era one takes the shared upload token. The addon does not probe which
    era it is talking to, so precedence is the whole decision.
    """

    def test_the_session_token_wins_when_signed_in(self):
        self.assertEqual(transport.credential("sess-abc", "dev"), "sess-abc")

    def test_falls_back_to_the_upload_token_when_not_signed_in(self):
        self.assertEqual(transport.credential("", "dev"), "dev")
        self.assertEqual(transport.credential(None, "dev"), "dev")

    def test_whitespace_is_not_a_credential(self):
        # A pref field a user cleared by selecting-and-space must not shadow the token that
        # would actually work.
        self.assertEqual(transport.credential("   ", "dev"), "dev")

    def test_session_first_is_the_load_bearing_order(self):
        # The static token DEFAULTS to 'dev', so it is almost always non-empty. Preferring it
        # would mean a signed-in user silently kept sending the anonymous credential — and
        # their uploads would land outside their own quota and ?mine listing.
        self.assertEqual(transport.credential("sess-abc", "dev"), "sess-abc")

    def test_neither_is_empty_not_an_error(self):
        # A read-only page has no credential at all; the caller decides what to do.
        self.assertEqual(transport.credential(None, None), "")

if __name__ == "__main__":  # pragma: no cover
    unittest.main()


class EndpointNormalisation(unittest.TestCase):
    """A configured endpoint with a trailing slash must still hit the routes.

    The upload POST was the ONE call site that passed `endpoint` raw while
    blob_exists / stats / the token probe / delete_asset all rstrip'd it. The
    service matches the upload route as an exact `^…/api/assets$`, so the stray
    slash 404s — and hint_for(404) then blames a missing asset id, sending the
    user looking in entirely the wrong place (bd showcade-xlly).
    """

    def _capture(self, endpoint):
        seen = {}

        def fake_request(method, url, **kwargs):
            seen[method] = url
            if method == "HEAD":
                return transport.Response(status=404, body=b"")
            return transport.Response(status=201, body=b'{"id":"A"}')

        original = transport.request
        transport.request = fake_request
        try:
            transport.upload_model(
                endpoint,
                "tok",
                name="Thing",
                data=b"glTF\x02\x00\x00\x00" + b"\x00" * 8,
            )
        finally:
            transport.request = original
        return seen

    def test_upload_posts_to_the_bare_endpoint(self):
        seen = self._capture("http://h/api/assets/")
        self.assertEqual(seen["POST"], "http://h/api/assets")

    def test_a_slashless_endpoint_is_unchanged(self):
        seen = self._capture("http://h/api/assets")
        self.assertEqual(seen["POST"], "http://h/api/assets")

    def test_the_blob_probe_also_normalises(self):
        seen = self._capture("http://h/api/assets/")
        self.assertTrue(seen["HEAD"].startswith("http://h/api/assets/blob/"))
        self.assertNotIn("//blob/", seen["HEAD"])
