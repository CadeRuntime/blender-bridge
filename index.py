# SPDX-FileCopyrightText: 2026 caderuntime <caderuntime@cade.run>
# SPDX-License-Identifier: GPL-3.0-or-later

"""index.py — write the Blender extension-repository listing for a built zip.

A Blender 4.2+ extension repository is one static file. `index.json` names each
available extension along with the URL, byte size and sha256 of its zip; a user
pastes the URL of that file into Blender ▸ Preferences ▸ Get Extensions ▸
Repositories once, and Blender installs from it and re-checks it for updates
forever after. There is no server, no API and no account.

    python3 index.py \\
        --zip dist/showcade_bridge-0.1.0.zip \\
        --archive-url "$RELEASE/download/v0.1.0/showcade_bridge-0.1.0.zip" \\
        --out site/index.json

where `$RELEASE` is `https://github.com/caderuntime/blender-bridge/releases`.
`.github/workflows/release.yml` is the caller that matters; it composes that URL
from the tag and the built zip's name.

THE METADATA IS READ OUT OF THE ZIP, not out of the working tree. A
`blender_manifest.toml` in a checkout describes what the next build *would* be;
the copy inside the archive describes what users will actually install, and those
two differ the moment anyone edits the manifest after a release is cut. Reading
the artifact is also exactly what Blender's own generator does.

FORMAT SOURCE — this shape is not invented. It is what `blender_ext.py
server-generate` emits (Blender's own repository generator, bundled with every
install at `scripts/addons_core/bl_pkg/cli/blender_ext.py`): a `"v1"` envelope, an
empty blocklist, and one entry per package carrying the manifest fields in
`PkgManifest` declaration order with the `archive_*` triple appended.
`test/test_index.py` PROVES the match by running that generator over the same zip
and diffing, on any machine with a Blender installed — so an upstream format
change fails a test instead of silently publishing a repository Blender cannot
read. Key ORDER is not part of the contract (Blender parses JSON), but matching it
makes that diff exact and free.

WHY NOT JUST RUN BLENDER'S GENERATOR IN CI. Two reasons. It only ever writes a
RELATIVE `archive_url` (`./showcade_bridge-0.1.0.zip`), because it assumes the
zips sit beside the index — and ours deliberately do not: the index is served from
Cloudflare Pages at extensions.cade.run while the zips live in GitHub Releases (bd
showcade-8i87.11), so the URL must be absolute. And it would mean installing a
~300MB Blender into the release job to write 30 lines of JSON.

ONLY THE LATEST RELEASE IS LISTED, deliberately. Blender's repository format can
carry several versions of one extension, but it offers the user the newest
regardless, so listing history buys nothing and costs a release job that has to
fetch and re-hash every prior zip. Older zips stay downloadable at their Release
URLs forever; they are simply not advertised. Listing more than one is the reason
this writes a `data` LIST rather than a single object — the change would be here
and in the release workflow, not a rewrite.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tomllib
import zipfile
from pathlib import Path
from typing import Any

MANIFEST_NAME = "blender_manifest.toml"

#: The envelope version. Blender's generator hardcodes this too; it is the
#: repository FORMAT version, unrelated to the extension's own version.
INDEX_VERSION = "v1"

#: Manifest fields that belong in a listing entry, in the order Blender's
#: `PkgManifest` declares them (required first, then optional). Fields absent
#: from the manifest are omitted rather than emitted as `null` — Blender's
#: generator drops `None` for the same reason.
#:
#: This is an ALLOW-LIST, not a passthrough, and that is load-bearing: a manifest
#: also carries build-time-only tables (`[build]`, with its
#: `paths_exclude_pattern`) which have no meaning to a client and which Blender's
#: generator does not emit either. A passthrough would publish them.
#:
#: The cost of an allow-list is that a NEW upstream field is silently omitted
#: until it is added here. That is precisely what the conformance test in
#: `test/test_index.py` catches — Blender's output would carry the field and ours
#: would not, and the diff fails.
REQUIRED_FIELDS = (
    "schema_version",
    "id",
    "name",
    "tagline",
    "version",
    "type",
    "maintainer",
    "license",
    "blender_version_min",
)

OPTIONAL_FIELDS = (
    "blender_version_max",
    "website",
    "copyright",
    "permissions",
    "tags",
    "platforms",
)

MANIFEST_FIELDS = REQUIRED_FIELDS + OPTIONAL_FIELDS


def read_manifest_from_zip(zip_path: Path) -> dict[str, Any]:
    """Parse `blender_manifest.toml` out of a built extension zip.

    The manifest is at the zip ROOT (Blender's loader unpacks the archive into a
    directory it names from the manifest `id`), which is what `package.py` builds.
    """
    with zipfile.ZipFile(zip_path) as archive:
        try:
            raw = archive.read(MANIFEST_NAME)
        except KeyError:
            raise SystemExit(
                f"{zip_path}: no {MANIFEST_NAME} at the zip root — this is not a Blender "
                f"4.2+ extension archive. Build it with `python3 package.py build`."
            ) from None
    return tomllib.loads(raw.decode("utf-8"))


def archive_hash(zip_path: Path, block_size: int = 1 << 20) -> str:
    """`sha256:<hex>` over the zip's bytes — the form Blender's index carries."""
    digest = hashlib.sha256()
    with open(zip_path, "rb") as handle:
        while chunk := handle.read(block_size):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def entry_for(zip_path: Path, archive_url: str) -> dict[str, Any]:
    """One `data` entry: the zip's manifest fields plus where to fetch it."""
    manifest = read_manifest_from_zip(zip_path)

    # FAIL CLOSED on wheels rather than publish a listing that lies. An extension
    # shipping Python wheels must also advertise the interpreter versions those
    # wheels are built for (`python_versions`, which Blender's generator derives
    # from the wheel FILENAMES) or Blender offers the extension to interpreters it
    # cannot run on. This addon is stdlib-only by design — pyproject.toml calls
    # taking a dependency a design change, not a convenience — so rather than
    # carry untested derivation code for a case that should never arrive, this
    # stops and says what to write.
    if manifest.get("wheels"):
        raise SystemExit(
            f"{zip_path}: the manifest declares `wheels`, which this generator does not "
            f"handle. A listing for a wheel-bearing extension also needs `python_versions` "
            f"derived from the wheel filenames (see `python_versions_from_wheels` in "
            f"Blender's blender_ext.py). Extend this file before releasing that."
        )

    entry = {field: manifest[field] for field in MANIFEST_FIELDS if field in manifest}

    missing = [field for field in REQUIRED_FIELDS if field not in entry]
    if missing:
        raise SystemExit(f"{zip_path}: manifest is missing required field(s): {', '.join(missing)}")

    entry["archive_url"] = archive_url
    entry["archive_size"] = zip_path.stat().st_size
    entry["archive_hash"] = archive_hash(zip_path)
    return entry


def build_index(zip_path: Path, archive_url: str) -> dict[str, Any]:
    """The whole `index.json` document for one published zip."""
    return {
        "version": INDEX_VERSION,
        # No extension is blocked. The field is REQUIRED even when empty — Blender
        # reports `missing "blocklist" field` and refuses the whole repository
        # without it, so this is not an empty gesture.
        "blocklist": [],
        "data": [entry_for(zip_path, archive_url)],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--zip", required=True, type=Path, help="the built extension zip")
    parser.add_argument(
        "--archive-url",
        required=True,
        help="absolute URL the zip is downloadable from (its GitHub Release asset)",
    )
    parser.add_argument(
        "--out",
        required=True,
        type=Path,
        help="where to write index.json; parent directories are created",
    )
    args = parser.parse_args(argv)

    # A RELATIVE URL HERE WOULD PUBLISH A BROKEN REPOSITORY, and it would look
    # fine: Blender resolves it against the index's own location, so
    # `./showcade_bridge-0.1.0.zip` becomes a 404 on the index host — which serves
    # only index.json — while the index itself parses perfectly. The failure would
    # reach users as "install failed" with nothing wrong on the server. Blender's
    # own generator emits exactly that relative form (its zips sit beside the
    # index); ours must not.
    if not args.archive_url.startswith(("https://", "http://")):
        raise SystemExit(
            f"--archive-url must be absolute (https://…), got {args.archive_url!r}. The zips "
            f"live in GitHub Releases and the index is served from another host entirely, so a "
            f"relative URL resolves against the index host and 404s."
        )

    index = build_index(args.zip, args.archive_url)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(index, indent=2) + "\n", encoding="utf-8")

    entry = index["data"][0]
    print(f"[index] {args.out}: {entry['id']} {entry['version']} ({entry['archive_size']} bytes)")
    print(f"[index]   {entry['archive_url']}")
    print(f"[index]   {entry['archive_hash']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
