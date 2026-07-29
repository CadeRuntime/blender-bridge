# SPDX-FileCopyrightText: 2026 caderuntime <caderuntime@cade.run>
# SPDX-License-Identifier: GPL-3.0-or-later

"""package.py — build the installable extension zip, or symlink it for dev.

Two subcommands, both stdlib-only and both runnable without Blender:

    python3 package.py build   # → dist/<id>-<ver>.zip
    python3 package.py link    # → symlink into the user extensions dir

`task blender:package` / `task blender:link` wrap them.

The zip is **byte-for-byte deterministic**: entries sorted, a fixed timestamp,
fixed permissions, `__pycache__` excluded. Not for its own sake — a build that
changes bytes on every run cannot be diffed, cached, or compared against the one
a colleague installed, and `__pycache__` in an extension zip is how a stale
`.pyc` from another Python version ends up shipped.

A Blender 4.2+ extension zip has `blender_manifest.toml` at the ZIP ROOT (the
loader unpacks it into a directory named by the manifest `id`), so the package
directory's *contents* are what gets zipped — not the directory itself.
"""

from __future__ import annotations

import argparse
import os
import sys
import tomllib
import zipfile
from pathlib import Path

ADDON_DIR = Path(__file__).resolve().parent
PACKAGE_DIR = ADDON_DIR / "showcade_bridge"
DIST_DIR = ADDON_DIR / "dist"
MANIFEST_NAME = "blender_manifest.toml"

#: Fixed zip timestamp (1980-01-01, the earliest the format can encode).
_ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)
_EXCLUDE_DIRS = frozenset({"__pycache__"})
_EXCLUDE_SUFFIXES = (".pyc", ".pyo")

#: Files taken from the ADDON ROOT (not the package dir) into the zip ROOT.
#:
#: The addon is GPL-3.0-or-later, and the GPL obliges us to ship the license
#: with the thing it licenses. The zip is the ONLY artifact a user ever
#: receives — nobody who installs from Blender's Install-from-Disk ever sees the
#: repository — so a LICENSE that lives only in the repo satisfies nothing.
#:
#: It is kept at the addon root rather than inside `showcade_bridge/` so that
#: after the blender-bridge extraction the file sits at the REPO root, which is
#: where GitHub looks to detect the licence. Root for humans, zip root for the
#: GPL; this constant is the bridge between the two (bd showcade-8i87.8).
SIDECAR_FILES = ("LICENSE",)


def read_manifest(package_dir: Path = PACKAGE_DIR) -> dict:
    with open(package_dir / MANIFEST_NAME, "rb") as handle:
        return tomllib.load(handle)


def payload_paths(package_dir: Path = PACKAGE_DIR) -> list[Path]:
    """Every file that belongs in the zip, as sorted package-relative paths."""
    found: list[Path] = []
    for root, dirnames, filenames in os.walk(package_dir):
        dirnames[:] = sorted(d for d in dirnames if d not in _EXCLUDE_DIRS and not d.startswith("."))
        for filename in filenames:
            if filename.startswith(".") or filename.endswith(_EXCLUDE_SUFFIXES):
                continue
            found.append((Path(root) / filename).relative_to(package_dir))
    return sorted(found)


def sidecar_paths(addon_dir: Path = ADDON_DIR) -> list[Path]:
    """`SIDECAR_FILES` that actually exist, as absolute paths, sorted by zip name.

    Missing sidecars are skipped rather than fatal: `link` installs the package
    directory directly and never consults these, so a checkout without them must
    still build. `test_packaging.py` is what asserts the LICENSE is really there
    — a build that silently shipped without it would be the failure that matters.
    """
    return [addon_dir / name for name in sorted(SIDECAR_FILES) if (addon_dir / name).is_file()]


def build(
    package_dir: Path = PACKAGE_DIR,
    dist_dir: Path = DIST_DIR,
    addon_dir: Path = ADDON_DIR,
) -> Path:
    """Write the extension zip and return its path."""
    manifest = read_manifest(package_dir)
    dist_dir.mkdir(parents=True, exist_ok=True)
    out = dist_dir / f"{manifest['id']}-{manifest['version']}.zip"

    # zip name -> source path. The package wins a name collision, so a LICENSE
    # placed inside the package would not be shadowed by the sidecar (and would
    # not be written twice, which is what a duplicate zip entry would be).
    entries: dict[str, Path] = {
        relative.as_posix(): package_dir / relative for relative in payload_paths(package_dir)
    }
    for path in sidecar_paths(addon_dir):
        entries.setdefault(path.name, path)

    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name in sorted(entries):  # sorted over the MERGED set, so still deterministic
            info = zipfile.ZipInfo(name, date_time=_ZIP_EPOCH)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16  # not the source file's mode
            archive.writestr(info, entries[name].read_bytes())
    return out


def extensions_dir(system: str, blender_version: str, *, home: Path, appdata: str | None = None) -> Path:
    """Where Blender 4.2+ looks for user-installed extensions, per OS.

    `appdata` is `%APPDATA%` and is only consulted on Windows; passing it in
    (rather than reading the environment here) keeps this a pure function.
    """
    if system == "Darwin":
        base = home / "Library" / "Application Support" / "Blender"
    elif system == "Windows":
        base = Path(appdata or (home / "AppData" / "Roaming")) / "Blender Foundation" / "Blender"
    else:
        base = home / ".config" / "blender"
    return base / blender_version / "extensions" / "user_default"


def installed_versions(system: str, *, home: Path, appdata: str | None) -> list[str]:
    """Blender version dirs that actually exist, newest first.

    A version dir only exists once that Blender has run at least once, which is
    exactly the condition under which a symlink into it will be read.
    """
    # extensions_dir() appends "<ver>/extensions/user_default"; walk back to the base.
    base = extensions_dir(system, "x", home=home, appdata=appdata).parent.parent.parent
    if not base.is_dir():
        return []
    versions = [d.name for d in base.iterdir() if d.is_dir() and d.name[:1].isdigit()]
    return sorted(versions, key=lambda v: [int(p) for p in v.split(".") if p.isdigit()], reverse=True)


def link(blender_version: str, *, package_dir: Path = PACKAGE_DIR) -> Path:
    """Symlink the working tree into the extensions dir — edit, restart, done."""
    import platform

    system = platform.system()
    home = Path.home()
    appdata = os.environ.get("APPDATA")

    # Resolve the version BEFORE creating anything. mkdir(parents=True) would
    # happily fabricate ~/.config/blender/4.2/… on a machine that only runs 5.2,
    # symlink into it and report success — a link no Blender ever reads
    # (bd showcade-qg9y). Fail, or auto-pick, instead of inventing a directory.
    present = installed_versions(system, home=home, appdata=appdata)
    if not blender_version:
        if not present:
            raise SystemExit(
                "no Blender config directory found — run Blender once, or pass "
                "--blender-version explicitly"
            )
        blender_version = present[0]
        print(f"[blender:link] auto-detected Blender {blender_version}")
    elif present and blender_version not in present:
        raise SystemExit(
            f"Blender {blender_version} has no config directory; installed: {', '.join(present)}.\n"
            f"Re-run with --blender-version {present[0]} (or BLENDER_VERSION={present[0]})."
        )

    target = extensions_dir(system, blender_version, home=home, appdata=appdata)
    target.mkdir(parents=True, exist_ok=True)
    destination = target / package_dir.name
    if destination.is_symlink() or destination.exists():
        if destination.is_symlink():
            destination.unlink()
        else:
            raise SystemExit(f"{destination} exists and is not a symlink — remove it first")
    destination.symlink_to(package_dir, target_is_directory=True)
    return destination


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("build", help="write the extension zip into dist/")
    link_parser = sub.add_parser("link", help="symlink the working tree into Blender's extensions dir")
    link_parser.add_argument(
        "--blender-version",
        default="",
        help="e.g. 5.2; empty auto-detects the newest installed Blender",
    )
    args = parser.parse_args(argv)

    if args.command == "build":
        out = build()
        print(f"[blender] packaged {out.relative_to(ADDON_DIR.parents[1])} ({out.stat().st_size} bytes)")
        return 0
    destination = link(args.blender_version)
    print(f"[blender] linked {destination} -> {PACKAGE_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
