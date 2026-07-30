# SPDX-FileCopyrightText: 2026 caderuntime <caderuntime@cade.run>
# SPDX-License-Identifier: GPL-3.0-or-later

"""The extension-repository listing (`index.py`, `index.json`).

`index.json` is the whole distribution channel: it is the only thing Blender
fetches to discover that a new version exists, and the URL it is served from is
pasted into a user's preferences ONCE and re-read forever. A malformed index does
not fail loudly at release time — it fails later, on other people's machines, as
"repository unavailable" or a silently missing update.

Two kinds of check, and the second is the one with teeth:

* **structural** — the fields Blender requires are present and well formed, the
  hash and size describe the actual bytes, the URL is absolute. These run
  everywhere, including CI, and need nothing installed.
* **conformance** — the generated document is diffed against what BLENDER'S OWN
  generator (`blender_ext.py server-generate`) writes for the same zip. That is
  the real specification, and this is what stops `index.py` from drifting away
  from it: a field Blender starts emitting, or renames, fails here. It needs a
  Blender on PATH, so it SKIPS in CI (no Blender in a python:3.11-slim image) and
  runs for anyone developing the release path locally, which is where a format
  change would be noticed and fixed.

The skip is deliberate rather than lazy: the alternative is a ~300MB download in
every CI run to check a format that changes about once per Blender major release.
Same shape as showcade's `lint:oracle-conformance`, which likewise verifies
against a pinned upstream when it can reach it and says so plainly when it cannot.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import fake_assets  # noqa: F401  (puts the repo root on sys.path)

import index
import package

#: A stand-in for the real GitHub Release asset URL. Absolute, and pointedly NOT
#: on the index's own host — that is the arrangement being tested.
ARCHIVE_URL = "https://github.com/caderuntime/blender-bridge/releases/download/v9.9.9/showcade_bridge-9.9.9.zip"


def build_zip(into: Path) -> Path:
    """Build the real extension zip into a temp dist dir and return its path."""
    return package.build(dist_dir=into)


class Structure(unittest.TestCase):
    """What Blender requires of the document, checked without Blender."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.zip_path = build_zip(Path(cls._tmp.name))
        cls.index = index.build_index(cls.zip_path, ARCHIVE_URL)
        cls.entry = cls.index["data"][0]

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_the_envelope(self):
        self.assertEqual(self.index["version"], "v1")
        # REQUIRED even when empty: Blender reports `missing "blocklist" field`
        # and rejects the entire repository if it is absent.
        self.assertEqual(self.index["blocklist"], [])
        self.assertEqual(len(self.index["data"]), 1)

    def test_the_required_manifest_fields_are_carried(self):
        for field in index.REQUIRED_FIELDS:
            self.assertIn(field, self.entry, field)
        self.assertEqual(self.entry["id"], "showcade_bridge")
        self.assertEqual(self.entry["type"], "add-on")

    def test_the_network_permission_survives_into_the_listing(self):
        # The permission is what makes uploads possible at all; if it were dropped
        # from the listing the extension would install from the repository and
        # every send would fail with what reads like a transport bug.
        self.assertTrue(self.entry["permissions"]["network"])

    def test_build_time_tables_are_not_published(self):
        # `[build]` (paths_exclude_pattern) is meaningful only to the packager.
        # Blender's generator does not emit it; a passthrough implementation would.
        self.assertNotIn("build", self.entry)

    def test_the_archive_triple_describes_the_real_bytes(self):
        self.assertEqual(self.entry["archive_url"], ARCHIVE_URL)
        self.assertEqual(self.entry["archive_size"], self.zip_path.stat().st_size)
        algorithm, _, digest = self.entry["archive_hash"].partition(":")
        self.assertEqual(algorithm, "sha256")
        self.assertEqual(len(digest), 64)
        self.assertEqual(digest, digest.lower())
        self.assertRegex(digest, r"^[0-9a-f]{64}$")

    def test_the_version_matches_the_zip_it_describes(self):
        # The index is generated FROM THE ARCHIVE, so this holds even if the
        # working tree's manifest has moved on since the zip was built.
        self.assertIn(self.entry["version"], self.zip_path.name)

    def test_a_relative_archive_url_is_refused(self):
        # Blender resolves a relative URL against the index's own host, which
        # serves index.json and nothing else — so this would publish a repository
        # that parses perfectly and 404s on install.
        with self.assertRaises(SystemExit):
            index.main(
                [
                    "--zip",
                    str(self.zip_path),
                    "--archive-url",
                    "./showcade_bridge-9.9.9.zip",
                    "--out",
                    str(self.zip_path.parent / "index.json"),
                ]
            )

    def test_a_zip_without_a_manifest_is_refused(self):
        import zipfile

        empty = self.zip_path.parent / "not-an-extension.zip"
        with zipfile.ZipFile(empty, "w") as archive:
            archive.writestr("README", "nothing here")
        with self.assertRaises(SystemExit):
            index.build_index(empty, ARCHIVE_URL)

    def test_it_writes_the_file_and_round_trips(self):
        out = self.zip_path.parent / "site" / "index.json"
        self.assertEqual(
            index.main(["--zip", str(self.zip_path), "--archive-url", ARCHIVE_URL, "--out", str(out)]),
            0,
        )
        self.assertEqual(json.loads(out.read_text()), self.index)


@unittest.skipIf(
    shutil.which("blender") is None,
    "no `blender` on PATH — the conformance check needs Blender's own generator "
    "(this is expected in CI; run the suite locally to exercise it)",
)
class ConformanceWithBlender(unittest.TestCase):
    """Diff our listing against `blender_ext.py server-generate`, the real spec."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo_dir = Path(self._tmp.name)
        self.zip_path = build_zip(self.repo_dir)
        self.addCleanup(self._tmp.cleanup)

    def blender_index(self) -> dict:
        """Run Blender's generator over the zip and parse what it wrote.

        Skips (rather than fails) when Blender is present but unrunnable here — a
        broken local install should not read as a format violation. A generator
        that RUNS and disagrees is the failure this class exists for.
        """
        try:
            completed = subprocess.run(
                ["blender", "--command", "extension", "server-generate", f"--repo-dir={self.repo_dir}"],
                capture_output=True,
                text=True,
                timeout=180,
                # Blender writes to the user's config dirs on startup; keep it out
                # of the developer's real profile.
                env={**os.environ, "BLENDER_USER_RESOURCES": str(self.repo_dir / "blender-profile")},
            )
        except (OSError, subprocess.TimeoutExpired) as exception:
            raise unittest.SkipTest(f"could not run Blender's generator: {exception}") from None

        written = self.repo_dir / "index.json"
        if completed.returncode != 0 or not written.is_file():
            raise unittest.SkipTest(
                f"Blender's generator did not produce an index (exit {completed.returncode}): "
                f"{completed.stderr.strip()[:400]}"
            )
        return json.loads(written.read_text())

    def test_our_listing_matches_blenders(self):
        theirs = self.blender_index()
        ours = index.build_index(self.zip_path, ARCHIVE_URL)

        # The ONE deliberate divergence: Blender's generator assumes the zips sit
        # beside the index and writes a relative URL. Ours are in GitHub Releases.
        # Normalise that away so everything else is compared honestly.
        self.assertEqual(theirs["data"][0]["archive_url"], "./" + self.zip_path.name)
        theirs["data"][0]["archive_url"] = ARCHIVE_URL

        self.assertEqual(ours, theirs)

    def test_the_field_set_and_order_match(self):
        # Key order is NOT part of the contract (Blender parses JSON), but a
        # mismatch means our transcription of `PkgManifest`'s declaration order has
        # drifted — which is the same signal, caught earlier and read more easily
        # than a whole-document diff.
        theirs = self.blender_index()
        ours = index.build_index(self.zip_path, ARCHIVE_URL)
        self.assertEqual(list(ours["data"][0]), list(theirs["data"][0]))
        self.assertEqual(list(ours), list(theirs))


if __name__ == "__main__":
    unittest.main()
