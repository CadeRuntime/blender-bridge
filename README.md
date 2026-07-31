# showcade_bridge — send a model from Blender to showcade

A Blender addon that exports your selection to **GLB** and uploads it to a
[showcade](https://show.cade.run) asset catalog, so a model authored in Blender
can be bound to a table device from showcade's object editor. Nothing in the
browser needs changing on this path.

Re-export and send again and the binding follows the new bytes — the addon keeps
a stable identity on the collection, so showcade always resolves the current
version.

- **Blender 4.2 or newer** (it is a 4.2+ *extension*, not a legacy addon)
- No third-party Python dependencies — standard library only
- Licence: **GPL-3.0-or-later**

> **This repository is a mirror.** Development happens on a canonical upstream
> that is not reachable from here, and the mirror is **write-only from our side,
> and it overwrites**: anything committed or merged *in this repository* — a merge
> commit, a hotfix, a branch, by anyone including us — is destroyed by the next
> push from upstream. Nothing is ever merged here. Pull requests are landed
> upstream and arrive back by mirror, which means yours will be *closed* rather
> than merged, with your authorship intact. That is not a rejection.
>
> **Your own fork is untouched by any of this** — its branches live in your
> repository, not in ours, and the mirror never reaches them. Work there as
> normal. [CONTRIBUTING.md](CONTRIBUTING.md) walks the whole path, including why
> a pull request can sit open for a while after its code is already on `main`.

## Install

### From the extension repository — the way that gets updates

> **Not live until the first release is cut.** The URL below does not answer yet;
> build from source in the meantime. This paragraph goes away with `0.1.0`.

Blender ▸ Preferences ▸ **Get Extensions** ▸ Repositories ▸ **+** ▸ *Add Remote
Repository*, and paste:

```
https://extensions.cade.run/index.json
```

Then find **Showcade Bridge** in the extension list and install it. That is the
whole setup — Blender re-reads that URL from then on, so a new version arrives as
an available update rather than as something you have to go looking for.

The repository is one static JSON listing, and the zips it points at live in
[Releases](https://github.com/caderuntime/blender-bridge/releases), built by public
CI from public source on the tag. Installing this way tells us nothing about you:
Blender fetches a file and a zip, and neither request carries an identity.

### From a zip

Take the `.zip` from the [latest
release](https://github.com/caderuntime/blender-bridge/releases/latest), then
Blender ▸ Preferences ▸ Add-ons ▸ **Install from Disk** ▸ pick it. Same extension —
you just have to come back yourself when there is a newer one.

### From source

```bash
python3 package.py build          # → dist/showcade_bridge-<version>.zip
```

…then Install from Disk as above. The zip is byte-for-byte deterministic, so a
build from a release tag is directly comparable with the one attached to that
Release.

For a development loop, symlink the working tree into Blender's extensions
directory and restart Blender after an edit rather than re-packaging:

```bash
python3 package.py link                          # newest installed Blender
python3 package.py link --blender-version 5.2    # …or a specific one
```

The manifest's `permissions.network` is not decoration: without it the extension
installs perfectly and every send fails with a permission error that reads like a
transport bug.

## Point it at a catalog

The addon talks to any showcade deployment over HTTP. Which one, and how you
authenticate, are the two things to set before the first send.

Open **Preferences ▸ Add-ons ▸ Showcade Bridge** and set the **endpoint** to that
deployment's asset API. For the hosted product that is:

```
https://show.cade.run/api/assets
```

Then choose a credential. There are two kinds, and a deployment accepts **one or
the other** — never both, and there is no falling back from one to the other:

- **Sign in** (below) — for a deployment that authenticates users. This is what
  the hosted product uses, and it is the normal path.
- **Upload token** — a shared secret, for a deployment configured with one.
  Typically a local or private instance.

Press **Test Connection**. It checks reach and the credential without writing
anything, and its failure message names the fix rather than the status code.

### Signing in

**Sign in** (preferences ▸ Account) runs the OAuth 2.0 **device grant**
(RFC 8628), the flow built for exactly this situation — an application with no
browser and no cookie jar:

1. The addon asks the auth service for a short user code.
2. It opens your browser at the approval page and shows you the code.
3. You sign in there as usual and approve.
4. The addon's next poll receives a session token and stores it in preferences.

From then on your uploads are attributed to **you** — they land inside your own
allowance and appear in your own listing rather than anonymously. No loopback
listener, no embedded browser, and the addon never sees a password.

Set **auth endpoint** to the deployment's auth API; for the hosted product that
is `https://show.cade.run/api/auth`.

**A session expires; a token does not.** A `401` some time after signing in
usually means the session lapsed — press **Sign in** again. The addon's hint
names both causes, because the two credentials fail identically from outside.

### Sending

In Blender: **View3D ▸ N-sidebar ▸ Showcade**.

1. Name the asset — it defaults to the active collection's name.
2. Press **Send to Showcade**, or `Ctrl+Shift+E` in object mode.
3. In showcade's object editor, right-click the device ▸ *Bind asset…* and pick it.

Identity lives on the **collection**: the first send mints a link id, stores it in
the `.blend`, and stamps it on every upload as a tag. That tag is how showcade
finds the current bytes for a binding on later sends. *New* mints a fresh
identity; *Unlink* forgets it.

### Preferences

| Preference | Default | What it changes |
|------------|---------|-----------------|
| `endpoint` | `http://localhost:8787/api/assets` | the catalog's asset API |
| `token` | `dev` | bearer token for writes, for a deployment that uses one |
| `auth_endpoint` | `http://localhost:8787/api/auth` | auth API base, for **Sign in** |
| `session_token` | *(empty)* | filled in by **Sign in**; takes precedence over `token` |
| `timeout_s` | 30 | per-request timeout; sends are off-thread, so this never freezes the UI |
| `gc_previous` | on | after a send, delete the asset **two** generations back |
| `insecure_tls` | off | skip TLS verification, for an endpoint whose certificate your machine does not trust |
| `auto_send_on_save` | off | re-send on every save of an already-linked, already-named collection |

The defaults point at `localhost:8787`, which suits a catalog run on your own
machine. For the hosted product, change both endpoints as above.

## The catalog contract

The upload behaviour this addon relies on — multipart part classification, the
`(sha256, name)` dedupe, the hash-first handshake that skips sending bytes the
catalog already holds, tag semantics, the limits and the status codes — is
published as an API reference:

**<https://cade.run/api/assets-http/>**

That page is authoritative. Nothing here restates it, deliberately: a second copy
is how the two drift apart.

## Scripted use

Works from Blender's Python console with this directory on `sys.path`:

```python
from showcade_bridge.send import send_selection
send_selection(endpoint="https://show.cade.run/api/assets", token="…", name="Bumper")
```

## Layout

| File | Needs `bpy`? | What |
|------|--------------|------|
| `showcade_bridge/__init__.py` | no | `bl_info` + `register()`/`unregister()` (submodules imported lazily) |
| `showcade_bridge/transport.py` | **no** | the catalog HTTP client — multipart upload, HEAD dedupe, delete, credential precedence |
| `showcade_bridge/deviceauth.py` | **no** | the RFC 8628 device grant — request a user code, poll for a session |
| `showcade_bridge/exporter.py` | **no** | the glTF exporter kwargs + the RNA filter |
| `showcade_bridge/policy.py` | **no** | which objects to export, the warnings, the result line |
| `showcade_bridge/worker.py` | **no** | the off-thread send + its non-blocking drain |
| `showcade_bridge/send.py` | no¹ | export ∘ upload; `send_bytes()` is `bpy`-free |
| `showcade_bridge/export_glb.py` | yes | `bpy.ops.export_scene.gltf` → GLB bytes |
| `showcade_bridge/props.py` | yes | `ShowcadeLink` — the identity stored on a Collection |
| `showcade_bridge/prefs.py` | yes | preferences + the `Ctrl+Shift+E` keymap item |
| `showcade_bridge/ops.py` | yes | the seven operators — *Send to Showcade*, *Test Connection*, *Sign in*, *Sign out*, *New Link*, *Unlink*, *Copy Handle* |
| `showcade_bridge/panel.py` | yes | `VIEW3D_PT_showcade` (N-sidebar ▸ Showcade) |
| `showcade_bridge/blender_manifest.toml` | — | the 4.2+ extension manifest |
| `package.py` | no | build the zip / symlink for development |
| `test/` | no | plain `unittest` |

¹ `send_selection()` imports `export_glb` *inside* the function, so the module
itself imports fine without Blender.

**The `bpy`-free split is load-bearing, not stylistic.** It is what lets the whole
transport, every send decision and the whole thread hand-off be tested without
Blender in the loop — keep `bpy` out of `transport.py`, `exporter.py`,
`policy.py` and `worker.py`, and keep them stdlib-only (no `requests`). For
`worker.py` it is also the actual thread-safety contract: the worker callable runs
off the main thread, where touching `bpy` data is undefined behaviour.

## Things that will bite you

- **Keep the asset name stable across re-sends.** Dedupe is by `(sha256, name)`,
  so an unchanged re-export returns the existing row and writes nothing. Putting a
  timestamp or a version in the name throws that away.
- **The addon never bakes a scale.** The GLB is true Blender metres; the browser
  converts metres to table units, because only it knows the target device's
  footprint. If `scene.unit_settings.scale_length != 1.0` your metre is not a
  metre and the export is off by that factor — fix it in Blender.
- **All three compression paths must stay off** (Draco, native meshopt, and
  `gltfpack`). showcade registers no decoder-backed glTF extensions, so a
  compressed GLB is one it *silently fails to load*. Draco and `gltfpack` exist in
  every exporter from 4.2 on and their absence **fails** the export; the native
  meshopt switch is **5.2 and newer only**, so on 4.2–5.1 it is reported as a
  dropped kwarg rather than refused — that exporter has no meshopt path to
  disable. The version table lives in `exporter.py` and is asserted by
  `DeclaredBlenderFloor` in `test/test_exporter.py`.
- **An override naming a compression switch is refused, and says so.** The clamp
  is not silent: `resolve_export_kwargs` logs the refused names, because a setting
  that did not take effect is the one failure the RNA filter cannot prevent.
- **The >5 m size warning measures the whole selection**, not each object's own
  box — "I selected the entire level" is many *small* objects spread over 30 m,
  which is exactly what the warning is for.
- **Source is the selection, and an empty selection is never the whole scene.**
  With nothing selected it falls back to the active collection; if that is empty
  too the operator reports an error and cancels. *Whole Scene* is a deliberate
  pick, not an accident.
- **The hotkey is rebindable.** `Ctrl+Shift+E` is registered in the *Object Mode*
  keymap and drawn in preferences with the stock widget, because a collision with
  another addon is a matter of time.
- **Uploads are synchronous under the hood.** `urllib` has no async mode and
  Blender's UI thread must not block on a socket: `ops.py` exports on the main
  thread, hands the resulting *bytes plus config strings* to `worker.SendJob`, and
  drains it from a modal timer that passes every non-timer event through. A worker
  must never touch `bpy` data — that is why what crosses the boundary is bytes,
  not the scene.
- **A dedupe is not a new generation.** The dedupe response must not rotate the GC
  window, or the next send deletes the asset the browser is currently loading —
  one generation back instead of two.
- **`wm.clipboard` reads back empty under `--background`.** There is no windowing
  system to own a selection, so *Copy Handle* can only be verified in a GUI
  session.

## Tests

```bash
PYTHONPATH=. python3 -m unittest discover -s test -t test
```

Runs the pure-unit tests — the source rules, the size and scale warnings, the GC
window, the multipart encoding, the worker's non-blocking exactly-once drain and
the reproducible extension zip — plus the upload contract end to end against a
catalog on an ephemeral port: fresh upload, identical re-send, changed bytes, the
HEAD-hit metadata-only path, a bad credential, an oversize model, the
side-effect-free connection probe, and delete reclaiming the blob.

That contract suite runs **twice**: against a stdlib fake, which needs nothing but
Python and always runs, and against a real catalog service, which is
authoritative and soft-skips when its runtime is absent. Neither replaces the
other — the fake keeps the suite runnable anywhere, the real service keeps it
honest, and drift between them fails here.

`python3 -m compileall .` is the fast syntax gate; `ruff` and `mypy` cover the
`bpy`-free modules (configured in `pyproject.toml`). The `bpy` half is *mostly*
out of reach, but `test/fake_bpy.py` is a shim big enough to import `props.py`
and `ops.py`, which covers the load-time status clear and the whole-selection
size measurement without Blender.

What remains uncoverable is registration against real RNA, the panel draw and the
modal timer. Verify those by hand after touching `ops.py`, `panel.py`, `props.py`
or `prefs.py`:

```bash
python3 package.py link && blender      # N-sidebar ▸ Showcade
```

One test class needs a **Blender on `PATH`** and skips without one:
`test_index.py`'s conformance check runs Blender's own repository generator
(`blender_ext.py server-generate`) over a freshly built zip and diffs it against
what `index.py` produces. That is how the published `index.json` stays a format
Blender actually reads, rather than one that looked right when it was written. It
skips in CI, where installing Blender to check a format that moves once per major
release is not worth the download — so if you touch `index.py`, run the suite
locally.

## Releases and support

Versions are [semver](https://semver.org); what changed in each is in
[CHANGELOG.md](CHANGELOG.md). A release is cut by tagging upstream. A workflow
**here** builds the zip and publishes the Release, so the artifact you install is
always one public CI built from public source, never a zip somebody uploaded from
a laptop — and because `package.py` is byte-deterministic, you can rebuild any
release from its tag and compare hashes yourself.

The extension repository index that advertises it is deployed separately, from
upstream, *after* verifying that the published zip is byte-identical to what this
source builds. So a release is announced to Blender only once its artifact has
been independently reproduced.

**Support is best effort, and there is no support commitment.** This is a bridge
between Blender and a product we run, published because it is more useful in the
open than closed, not as a supported deliverable. Issues and pull requests are
read and are genuinely welcome ([CONTRIBUTING.md](CONTRIBUTING.md) explains the
mirror mechanics), and there is no undertaking about response time, fixes, or
keeping any particular Blender version working. What *is* undertaken: the addon
declares `blender_version_min` honestly, tests run on the Python versions Blender
actually bundles, and a release never ships without the suite passing on the exact
tree being published.

## Licence

**GPL-3.0-or-later.** See [`LICENSE`](LICENSE); every source file carries an
`SPDX-License-Identifier` header. Copyright © 2026 caderuntime
(`caderuntime@cade.run`).

The addon links against `bpy`, so it is GPL by construction — this is not a
preference. GPL rather than AGPL is a deliberate pick: AGPL's distinguishing
clause covers *network interaction*, which a desktop addon never has, and the
accepted-licence list on `extensions.blender.org` is a real constraint that GPL
clears.

`LICENSE` lives at the addon root **and ships inside the packaged zip** —
`SIDECAR_FILES` in `package.py` puts it there, and `test_packaging.py` fails the
build if it is missing from either place. That is not tidiness: the zip is the
only artifact a user ever receives, so a licence that exists only in the
repository satisfies nothing.
