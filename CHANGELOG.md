# Changelog

Versions follow [semantic versioning](https://semver.org). The version lives in
`showcade_bridge/blender_manifest.toml` (and `bl_info`, which `test_packaging.py`
keeps in step); a release is cut by tagging `v<version>` on the canonical
upstream. The mirror carries the tag to GitHub, where a workflow builds the zip
and publishes the Release; upstream then fetches that published zip, checks it is
byte-identical to what this source builds, and deploys the extension repository
index describing it. See [CONTRIBUTING.md](CONTRIBUTING.md) for that path in full.

**There are deliberately no dates here.** GitHub records when each Release was
published and a hand-maintained date is one more thing to get wrong at the worst
moment. What this file records is what CHANGED, which nothing else knows.

The heading format matters: `## <version>`, exactly matching the tag with its `v`
stripped. The release workflow extracts the section under it for the Release
notes and **fails the release** if there is none — a version shipped with no note
about what changed is a version nobody can decide whether to install.

## 0.1.0

The first public release. Everything below is what the addon does as extracted
from showcade (bd showcade-8i87); it is not a list of changes against an earlier
public version, because there is none.

- Export the Blender selection to **GLB** and upload it to a
  [showcade](https://show.cade.run) asset catalog, so a model authored in Blender
  can be bound to a table device from showcade's object editor.
- A stable identity is kept on the collection, so re-exporting and sending again
  updates the same asset rather than creating a second one — the binding in
  showcade follows the new bytes.
- Uploads deduplicate by content hash: sending bytes the catalog already has
  costs a handshake instead of a transfer.
- Sign-in by device-code flow, so credentials are never typed into Blender.
- The upload runs off the main thread; Blender's UI does not block on the network.
- Standard library only, no third-party Python dependencies, and a test suite
  that runs on a clean clone with nothing installed.
- Blender 4.2+ as a proper extension (not a legacy addon), GPL-3.0-or-later.
