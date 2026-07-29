# SPDX-FileCopyrightText: 2026 caderuntime <caderuntime@cade.run>
# SPDX-License-Identifier: GPL-3.0-or-later

"""The extension manifest and the zip builder (`package.py`, `blender_manifest.toml`).

Blender 4.2+ reads `blender_manifest.toml`, not `bl_info`, and two of its fields
are silent-failure material:

* **`permissions.network`** — without it the extension installs perfectly and
  every send fails with a permission error that looks like a transport bug;
* **`version`** — drifting from `bl_info` means the legacy-addon install and the
  extension install report different versions of the same code.

The zip builder is asserted to be *deterministic* rather than compared to a
recorded byte string: a build that changes on every run cannot be diffed or
compared with the one a colleague installed.
"""

from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path

import fake_assets  # noqa: F401  (puts the repo root on sys.path)

import package
import showcade_bridge


class Manifest(unittest.TestCase):
    def setUp(self):
        self.manifest = package.read_manifest()

    def test_network_permission_is_declared(self):
        # 4.2's extension model DENIES network access unless this is present.
        network = self.manifest.get("permissions", {}).get("network")
        self.assertTrue(network, "permissions.network is missing — every upload would be blocked")
        self.assertIsInstance(network, str)

    def test_the_version_matches_bl_info(self):
        self.assertEqual(
            self.manifest["version"],
            ".".join(str(part) for part in showcade_bridge.bl_info["version"]),
        )

    def test_the_minimum_blender_matches_bl_info(self):
        declared = tuple(int(part) for part in self.manifest["blender_version_min"].split("."))
        legacy = tuple(showcade_bridge.bl_info["blender"])
        self.assertEqual(declared[:2], legacy[:2])

    def test_the_required_fields_are_present(self):
        for field in ("schema_version", "id", "name", "tagline", "maintainer", "type", "license"):
            self.assertIn(field, self.manifest, field)
        self.assertEqual(self.manifest["type"], "add-on")

    def test_the_tagline_obeys_blenders_rules(self):
        tagline = self.manifest["tagline"]
        self.assertLessEqual(len(tagline), 64)
        self.assertFalse(tagline.endswith("."), "Blender rejects a tagline ending in a period")

    def test_the_id_is_the_package_directory_name(self):
        # The loader unpacks the zip into a directory named by `id`; a mismatch
        # makes the relative imports inside the package fail after install.
        self.assertEqual(self.manifest["id"], package.PACKAGE_DIR.name)


class PayloadSelection(unittest.TestCase):
    def test_the_manifest_and_entry_point_are_at_the_zip_root(self):
        names = {path.as_posix() for path in package.payload_paths()}
        self.assertIn("blender_manifest.toml", names)
        self.assertIn("__init__.py", names)

    def test_bytecode_is_never_packaged(self):
        # `task test:blender` runs compileall, so __pycache__ exists locally; a
        # stale .pyc from another Python version must not ship.
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "pkg"
            (source / "__pycache__").mkdir(parents=True)
            (source / "__pycache__" / "x.cpython-312.pyc").write_bytes(b"\x00")
            (source / "stale.pyc").write_bytes(b"\x00")
            (source / ".hidden").write_text("no")
            (source / "__init__.py").write_text("")
            names = {path.as_posix() for path in package.payload_paths(source)}
        self.assertEqual(names, {"__init__.py"})

    def test_the_payload_order_is_stable(self):
        self.assertEqual(package.payload_paths(), sorted(package.payload_paths()))


class ZipBuild(unittest.TestCase):
    def build(self, dist: Path) -> Path:
        return package.build(dist_dir=dist)

    def test_two_builds_are_byte_identical(self):
        with tempfile.TemporaryDirectory() as one, tempfile.TemporaryDirectory() as two:
            first = self.build(Path(one)).read_bytes()
            second = self.build(Path(two)).read_bytes()
        self.assertEqual(first, second, "the extension zip is not reproducible")

    def test_rebuilding_over_an_existing_zip_is_idempotent(self):
        with tempfile.TemporaryDirectory() as dist:
            first = self.build(Path(dist)).read_bytes()
            second = self.build(Path(dist)).read_bytes()
        self.assertEqual(first, second)

    def test_the_zip_is_installable_shaped(self):
        with tempfile.TemporaryDirectory() as dist:
            out = self.build(Path(dist))
            self.assertIn(package.read_manifest()["version"], out.name)
            with zipfile.ZipFile(out) as archive:
                names = set(archive.namelist())
                self.assertIsNone(archive.testzip())
                # Root-level, not nested under a directory: the loader names the
                # install directory itself, from the manifest id.
                self.assertIn("blender_manifest.toml", names)
                self.assertIn("transport.py", names)
                self.assertFalse([n for n in names if "__pycache__" in n])


class LicenceShipsWithTheAddon(unittest.TestCase):
    """The GPL obliges the licence to travel WITH the thing it licenses.

    The zip is the only artifact a user ever receives — installing from disk
    never shows them the repository — so a LICENSE that exists only in the tree
    satisfies nothing. These are compliance tests, not packaging trivia
    (bd showcade-8i87.8).
    """

    def test_the_addon_root_has_a_licence(self):
        self.assertTrue(
            (package.ADDON_DIR / "LICENSE").is_file(),
            "LICENSE is missing — the addon is GPL-3.0-or-later",
        )

    def test_the_declared_licence_is_the_one_we_ship(self):
        declared = package.read_manifest()["license"]
        self.assertEqual(declared, ["SPDX:GPL-3.0-or-later"])
        text = (package.ADDON_DIR / "LICENSE").read_text()
        self.assertIn("GNU GENERAL PUBLIC LICENSE", text)
        self.assertIn("Version 3, 29 June 2007", text)
        # Affero is a DIFFERENT licence with a near-identical opening; showcade
        # itself is AGPL and copying the wrong file here would be invisible.
        self.assertNotIn("GNU AFFERO GENERAL PUBLIC LICENSE", text)

    def test_the_licence_is_in_the_zip_at_the_root(self):
        with tempfile.TemporaryDirectory() as dist:
            out = package.build(dist_dir=Path(dist))
            with zipfile.ZipFile(out) as archive:
                self.assertIn("LICENSE", set(archive.namelist()))
                self.assertIn(
                    "GNU GENERAL PUBLIC LICENSE",
                    archive.read("LICENSE").decode(),
                )

    def test_a_missing_sidecar_does_not_break_the_build(self):
        # `link` never consults sidecars, so a checkout without one must still
        # build; test_the_addon_root_has_a_licence is what makes its ABSENCE fail.
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as dist:
            empty = Path(tmp)
            out = package.build(dist_dir=Path(dist), addon_dir=empty)
            with zipfile.ZipFile(out) as archive:
                self.assertNotIn("LICENSE", set(archive.namelist()))
                self.assertIn("blender_manifest.toml", set(archive.namelist()))

    def test_every_source_file_carries_an_spdx_header(self):
        # Per-file identification is what lets a downstream packager or a licence
        # scanner classify a single vendored file. Missing it is silent.
        missing = []
        for path in sorted(package.ADDON_DIR.rglob("*.py")):
            if "__pycache__" in path.parts or "dist" in path.parts:
                continue
            head = path.read_text().split("\n\n")[0]
            if "SPDX-License-Identifier: GPL-3.0-or-later" not in head:
                missing.append(path.relative_to(package.ADDON_DIR).as_posix())
        self.assertEqual(missing, [], f"missing SPDX headers: {missing}")


class ExtensionsDirectory(unittest.TestCase):
    """The per-OS install path `task blender:link` symlinks into."""

    home = Path("/home/tester")

    def test_linux(self):
        self.assertEqual(
            package.extensions_dir("Linux", "4.2", home=self.home),
            self.home / ".config/blender/4.2/extensions/user_default",
        )

    def test_macos(self):
        self.assertEqual(
            package.extensions_dir("Darwin", "5.2", home=self.home),
            self.home / "Library/Application Support/Blender/5.2/extensions/user_default",
        )

    def test_windows_uses_appdata_when_it_is_set(self):
        self.assertEqual(
            package.extensions_dir("Windows", "4.2", home=self.home, appdata="C:/Users/t/AppData/Roaming"),
            Path("C:/Users/t/AppData/Roaming/Blender Foundation/Blender/4.2/extensions/user_default"),
        )

    def test_windows_falls_back_without_appdata(self):
        self.assertIn(
            "AppData",
            str(package.extensions_dir("Windows", "4.2", home=self.home, appdata=None)),
        )

    def test_an_unknown_platform_takes_the_linux_layout(self):
        # Better a wrong-but-obvious path than a crash on a BSD.
        self.assertIn(".config", str(package.extensions_dir("FreeBSD", "4.2", home=self.home)))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


class LinkVersionResolution(unittest.TestCase):
    """`link` must never invent a config dir for a Blender that has never run.

    mkdir(parents=True) made the old code fabricate ~/.config/blender/4.2/… on a
    5.2-only machine, symlink into it and report success — a link Blender never
    reads (bd showcade-qg9y).
    """

    def _home_with(self, versions):
        import tempfile

        tmp = tempfile.mkdtemp()
        root = Path(tmp)
        for v in versions:
            (root / ".config" / "blender" / v / "extensions" / "user_default").mkdir(parents=True)
        return root

    def test_versions_are_detected_newest_first(self):
        home = self._home_with(["4.2", "5.2", "4.10"])
        self.assertEqual(
            package.installed_versions("Linux", home=home, appdata=None),
            ["5.2", "4.10", "4.2"],  # numeric compare: 4.10 > 4.2
        )

    def test_no_blender_config_yields_no_versions(self):
        import tempfile

        self.assertEqual(
            package.installed_versions("Linux", home=Path(tempfile.mkdtemp()), appdata=None),
            [],
        )

    def test_non_version_directories_are_ignored(self):
        home = self._home_with(["5.2"])
        (home / ".config" / "blender" / "cache").mkdir(parents=True)
        self.assertEqual(package.installed_versions("Linux", home=home, appdata=None), ["5.2"])
