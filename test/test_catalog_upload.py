# SPDX-FileCopyrightText: 2026 caderuntime <caderuntime@cade.run>
# SPDX-License-Identifier: GPL-3.0-or-later

"""The transport against a live catalog, driven through the addon's own client.

Everything the addon depends on and cannot verify by reasoning: multipart part
classification, the `(sha256, name)` dedupe, the metadata-only handshake, the
auth statuses, the size cap, the revert generation, and the delete-time blob
reclaim.

The server here is `FakeAssetsServer` — standard library only, so this runs on a
clean clone with nothing installed.

**A fake alone cannot tell you it is right**, and that limit is worth stating
rather than hiding: it proves the addon agrees with an implementation written
from the same reading of the contract. The catalog's own repository runs this
same contract body against the REAL service, which is what catches the two
drifting apart. If you change an expectation here, you are asserting something
about the service — check it against
<https://cade.run/api/assets-http/>, which is the published contract.
"""

from __future__ import annotations

import unittest
import unittest.mock
from typing import Any
from urllib.parse import quote

from fake_assets import FakeAssetsServer, free_port, make_glb

from showcade_bridge import send, transport


class CatalogContract:
    """The assertions, with no opinion about which server answers them.

    Deliberately NOT a `TestCase`: unittest collects tests from `TestCase`
    subclasses, so this body runs exactly once per concrete backend below and
    never on its own.
    """

    server: Any

    @classmethod
    def make_server(cls) -> Any:
        raise NotImplementedError

    @classmethod
    def setUpClass(cls):
        cls.server = cls.make_server().start()

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()

    # ── helpers ──────────────────────────────────────────────────────────

    @property
    def endpoint(self) -> str:
        return self.server.endpoint

    def upload(self, name: str, data: bytes, *, token: str | None = None, **kw):
        return transport.upload_model(
            self.endpoint, token or self.server.token, name=name, data=data, timeout=30.0, **kw
        )

    def unique(self, suffix: str = "") -> str:
        return f"{self.id().rsplit('.', 1)[-1]}{suffix}"

    # ── the upload contract ──────────────────────────────────────────────

    def test_a_fresh_upload_creates_a_model_row(self):
        data = make_glb(self.unique())
        result = self.upload("Bumper-fresh", data, tags=transport.model_tags())
        self.assertEqual(result.status, 201)
        self.assertTrue(result.created)
        self.assertTrue(result.asset_id)
        self.assertEqual(result.sent_bytes, len(data), "the bytes should have crossed the wire")
        self.assertEqual(result.asset["kind"], "model")
        self.assertEqual(result.asset["mime"], "model/gltf-binary")
        self.assertEqual(result.asset["name"], "Bumper-fresh")
        self.assertEqual(result.asset["sha256"], transport.sha256_hex(data))
        self.assertEqual(result.asset["size"], len(data))

    def test_the_link_tag_round_trips_onto_the_row(self):
        link_id = transport.new_link_id()
        result = send.send_bytes(
            make_glb(self.unique()),
            endpoint=self.endpoint,
            token=self.server.token,
            name=self.unique("-asset"),
            link_id=link_id,
        )
        self.assertEqual(result.status, 201)
        self.assertIn(transport.link_tag(link_id), result.asset["tags"])
        self.assertIn("units:m", result.asset["tags"])
        self.assertIn("blender", result.asset["tags"])

    def test_an_identical_resend_is_idempotent(self):
        # Dedupe is (sha256, name): the same export under the same name must
        # return the SAME row, write nothing, and send no bytes the second time.
        data = make_glb(self.unique())
        name = self.unique("-asset")
        first = self.upload(name, data)
        second = self.upload(name, data)
        third = self.upload(name, data)
        self.assertEqual(first.status, 201)
        self.assertEqual(second.status, 200)
        self.assertEqual(third.status, 200)
        self.assertEqual(second.asset_id, first.asset_id)
        self.assertEqual(third.asset_id, first.asset_id)
        self.assertTrue(second.deduped)
        self.assertEqual(second.sent_bytes, 0, "HEAD hit ⇒ metadata-only, no bytes")

    def test_changed_bytes_make_a_new_generation(self):
        name = self.unique("-asset")
        first = self.upload(name, make_glb("gen-1"))
        second = self.upload(name, make_glb("gen-2"))
        self.assertEqual(second.status, 201)
        self.assertNotEqual(second.asset_id, first.asset_id)
        self.assertNotEqual(second.asset["sha256"], first.asset["sha256"])
        # …and the previous generation is still resolvable until GC removes it.
        self.assertTrue(transport.blob_exists(self.endpoint, first.asset["sha256"]))

    def test_a_revert_re_takes_the_newest_generation(self):
        # bd showcade-natd. v1 -> v2 -> v1, the shape of "undo the tweak and re-send".
        # Dedupe is (sha256, name), so the revert MATCHES v1's existing row — and
        # answering with it leaves v2's ULID highest, so the query the browser resolves
        # a link with (`?kind=model&tag=blender:<id>&limit=1`, newest first) kept serving
        # v2's bytes forever, with no further send able to recover it. The service now
        # mints a fresh generation row for the already-stored bytes instead.
        link_id = transport.new_link_id()
        name = self.unique("-asset")
        v1, v2 = make_glb("gen-1"), make_glb("gen-2")

        def send_gen(data: bytes):
            return send.send_bytes(
                data,
                endpoint=self.endpoint,
                token=self.server.token,
                name=name,
                link_id=link_id,
            )

        def resolved() -> dict:
            url = f"{self.endpoint}?kind=model&tag={quote(transport.link_tag(link_id))}&limit=1"
            resp = transport.request("GET", url, timeout=30.0)
            self.assertEqual(resp.status, 200)
            items = resp.json()["items"]
            self.assertEqual(len(items), 1, "a link must always resolve to exactly one row")
            return items[0]

        first, second = send_gen(v1), send_gen(v2)
        self.assertEqual(resolved()["id"], second.asset_id)

        back = send_gen(v1)
        self.assertEqual(back.status, 201, "a revert is a new generation, not a dedupe")
        self.assertNotIn(back.asset_id, (first.asset_id, second.asset_id))
        self.assertEqual(resolved()["id"], back.asset_id)
        self.assertEqual(resolved()["sha256"], transport.sha256_hex(v1))

        # Idempotent from here: re-sending what already resolves writes nothing.
        again = send_gen(v1)
        self.assertEqual(again.status, 200)
        self.assertEqual(again.asset_id, back.asset_id)
        self.assertEqual(resolved()["id"], back.asset_id)

    def test_the_head_hit_takes_the_metadata_only_path(self):
        # Same bytes, different name: the blob is already stored, so only
        # metadata crosses the wire but a new row is still created.
        data = make_glb(self.unique())
        first = self.upload(self.unique("-a"), data)
        self.assertEqual(first.sent_bytes, len(data))
        second = self.upload(self.unique("-b"), data)
        self.assertEqual(second.status, 201)
        self.assertEqual(second.sent_bytes, 0)
        self.assertEqual(second.asset["sha256"], first.asset["sha256"])
        self.assertNotEqual(second.asset_id, first.asset_id)

    def test_blob_exists_is_false_before_and_true_after(self):
        data = make_glb(self.unique())
        sha = transport.sha256_hex(data)
        self.assertFalse(transport.blob_exists(self.endpoint, sha))
        self.upload(self.unique("-asset"), data)
        self.assertTrue(transport.blob_exists(self.endpoint, sha))

    # ── failure modes ────────────────────────────────────────────────────

    def test_a_bad_token_is_401_with_a_token_hint(self):
        with self.assertRaises(transport.TransportError) as caught:
            self.upload(self.unique("-asset"), make_glb(self.unique()), token="not-the-token")
        self.assertEqual(caught.exception.status, 401)
        self.assertIn("token", caught.exception.hint)

    def test_an_oversize_glb_is_413(self):
        # The service caps models at 32 MB; the addon must surface that as a
        # named fix, not a stack trace.
        #
        # The addon now refuses an oversize GLB locally (bd showcade-81il) — the
        # unit suite pins that. This test is about the OTHER end: the service
        # enforcing its own cap, independently of the addon's mirror of it. So
        # lift the mirror for the duration and let the bytes actually go, which
        # is the only way to observe the 413 the hint text describes.
        oversize = make_glb("oversize").ljust(transport.MAX_MODEL_BYTES + 1024, b"\0")
        with (
            unittest.mock.patch.object(transport, "MAX_MODEL_BYTES", len(oversize) + 1),
            self.assertRaises(transport.TransportError) as caught,
        ):
            self.upload(self.unique("-asset"), oversize)
        self.assertEqual(caught.exception.status, 413)
        self.assertIn("32 MB", caught.exception.hint)  # the hint keeps the REAL cap

    # ── the connection probe ─────────────────────────────────────────────

    def test_the_probe_accepts_a_good_token_without_writing_anything(self):
        # `handlePatch` authorizes BEFORE the 404, so a PATCH at an id that
        # cannot exist is a side-effect-free token check. Prove the "no side
        # effect" half rather than assuming it: the catalog totals must not move.
        before = transport.probe(self.endpoint, self.server.token)
        self.assertTrue(before.ok, before.message)
        after = transport.probe(self.endpoint, self.server.token)
        self.assertEqual(after.assets, before.assets, "the probe created or deleted a row")
        self.assertEqual(after.bytes, before.bytes)

    def test_the_probe_rejects_a_bad_token_with_the_fix(self):
        result = transport.probe(self.endpoint, "not-the-token")
        self.assertFalse(result.ok)
        self.assertIn("token", result.message)

    def test_the_probe_reports_an_unreachable_catalog_as_such(self):
        # Wrong port: the failure must name `task dev:assets`, not a stack trace.
        dead = f"http://127.0.0.1:{free_port()}/api/assets"
        result = transport.probe(dead, self.server.token, timeout=2.0)
        self.assertFalse(result.ok)
        self.assertIn("dev:assets", result.message)

    # ── GC (the only storage-reclaim lever) ──────────────────────────────

    def test_delete_reclaims_the_blob_once_nothing_references_it(self):
        data = make_glb(self.unique())
        sha = transport.sha256_hex(data)
        kept = self.upload(self.unique("-kept"), data)
        doomed = self.upload(self.unique("-doomed"), data)

        # Two rows share the blob, so deleting one must NOT reclaim it.
        self.assertTrue(transport.delete_asset(self.endpoint, self.server.token, doomed.asset_id))
        self.assertTrue(transport.blob_exists(self.endpoint, sha))

        self.assertTrue(transport.delete_asset(self.endpoint, self.server.token, kept.asset_id))
        self.assertFalse(transport.blob_exists(self.endpoint, sha))

    def test_deleting_twice_is_not_fatal(self):
        # A GC pass runs unattended; an already-gone generation is normal.
        result = self.upload(self.unique("-asset"), make_glb(self.unique()))
        self.assertTrue(transport.delete_asset(self.endpoint, self.server.token, result.asset_id))
        self.assertFalse(transport.delete_asset(self.endpoint, self.server.token, result.asset_id))

    def test_deleting_with_a_bad_token_raises(self):
        result = self.upload(self.unique("-asset"), make_glb(self.unique()))
        with self.assertRaises(transport.TransportError) as caught:
            transport.delete_asset(self.endpoint, "not-the-token", result.asset_id)
        self.assertEqual(caught.exception.status, 401)


class CatalogTransport(CatalogContract, unittest.TestCase):
    """The contract against the stdlib fake — runs anywhere, skips never."""

    @classmethod
    def make_server(cls) -> FakeAssetsServer:
        return FakeAssetsServer()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
