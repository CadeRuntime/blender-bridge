# SPDX-FileCopyrightText: 2026 caderuntime <caderuntime@cade.run>
# SPDX-License-Identifier: GPL-3.0-or-later

"""The fake's own invariants — the ones `test_catalog_upload.py` cannot reach.

That file drives the fake through the addon's transport, which is the real test
of its behaviour. What it cannot check is the two properties the fake exists FOR:
that it stands alone (nothing installed, no catalog service to run), and
that its part classification is the rule the real server actually applies rather
than whatever the client happens to send.

Same role `test_server_harness.py` plays for the real harness (bd showcade-8i87.2).
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

from fake_assets import (
    FakeAssetsServer,
    make_glb,
    merge_tags,
    parse_multipart,
)


class StandsAlone(unittest.TestCase):
    """The whole point: this must work on a clean clone of the extracted repo."""

    @staticmethod
    def _imported_roots() -> set[str]:
        """Top-level module names `fake_assets` imports, read from its AST.

        The AST rather than `sys.modules`: what matters is what the file DECLARES
        it needs on a machine that has never run it, not what happens to be
        loaded in this interpreter — and by the time this test runs, the rest of
        the suite has imported plenty.
        """
        import ast

        import fake_assets

        tree = ast.parse(Path(fake_assets.__file__).read_text(encoding="utf-8"))
        roots: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                roots.add(node.module.split(".")[0])
        return roots

    def test_it_imports_nothing_outside_the_standard_library(self):
        # A third-party import here would silently reintroduce the coupling this
        # module was written to remove — and it would surface on a contributor's
        # machine, which is the one place we cannot debug.
        offenders = self._imported_roots() - set(sys.stdlib_module_names) - {"__future__"}
        self.assertEqual(offenders, set(), f"non-stdlib imports: {sorted(offenders)}")

    def test_it_does_not_import_the_harness_or_anything_local(self):
        # `server_harness` imports FROM here, never the other way round: the .5
        # cut lifts the harness out of the extracted repo, and a back-reference
        # would break the fake the moment it left. Checking "no local module at
        # all" rather than naming server_harness, so a future sibling cannot
        # sneak the coupling back under a different name.
        local = {p.stem for p in Path(__file__).parent.glob("*.py")}
        self.assertEqual(self._imported_roots() & local, set())

    def test_start_needs_no_bun_and_stop_is_idempotent(self):
        server = FakeAssetsServer()
        # Safe BEFORE start(), like AssetsServer.stop() — a suite that skips must
        # not blow up in tearDownClass.
        server.stop()
        server.start()
        self.assertIn("/api/assets", server.endpoint)
        server.stop()
        server.stop()


class PartClassification(unittest.TestCase):
    """A part with a filename is a FILE; one without is a STRING.

    This single rule is why the real-service test exists (Bun's `FormData` does
    exactly this), so the fake restating it correctly is what lets one contract
    body run against both servers.
    """

    def _body(self, extra: bytes = b"") -> tuple[bytes, str]:
        boundary = "----showcadetest"
        body = (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="meta"\r\n\r\n'
            '{"name":"x"}\r\n'
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="file"; filename="x.glb"\r\n'
            "Content-Type: model/gltf-binary\r\n\r\n"
        ).encode() + extra + f"\r\n--{boundary}--\r\n".encode()
        return body, f"multipart/form-data; boundary={boundary}"

    def test_the_named_part_without_a_filename_decodes_to_a_string(self):
        body, content_type = self._body(b"GLB")
        fields, files = parse_multipart(body, content_type)
        self.assertEqual(fields["meta"], '{"name":"x"}')
        self.assertNotIn("meta", files)

    def test_the_part_with_a_filename_decodes_to_bytes(self):
        payload = make_glb("classification")
        body, content_type = self._body(payload)
        fields, files = parse_multipart(body, content_type)
        self.assertEqual(files["file"], payload, "binary must survive byte-for-byte")
        self.assertNotIn("file", fields)

    def test_a_missing_boundary_is_an_error_not_a_silent_empty_parse(self):
        with self.assertRaises(ValueError):
            parse_multipart(b"whatever", "multipart/form-data")


class TagMerge(unittest.TestCase):
    """`None` means nothing new — the property that keeps a re-send write-free."""

    def test_nothing_new_is_none(self):
        self.assertIsNone(merge_tags(["a", "b"], ["b", "a"]))
        self.assertIsNone(merge_tags(["a"], []))

    def test_new_tags_append_in_arrival_order_keeping_existing_first(self):
        self.assertEqual(merge_tags(["a"], ["c", "b"]), ["a", "c", "b"])

    def test_duplicates_within_the_incoming_list_collapse(self):
        self.assertEqual(merge_tags([], ["x", "x", "y"]), ["x", "y"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
