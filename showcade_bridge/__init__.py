# SPDX-FileCopyrightText: 2026 caderuntime <caderuntime@cade.run>
# SPDX-License-Identifier: GPL-3.0-or-later

"""showcade_bridge — send a model from Blender to showcade's asset catalog.

View3D ▸ Sidebar (N) ▸ **Showcade**, or `Ctrl+Shift+E` in object mode: the
selection is exported to GLB and POSTed to the catalog, then bound in showcade
with the ordinary right-click ▸ *Bind asset…* picker. The shortcut is a real
`KeyMapItem` in the **Object Mode** keymap (`prefs.py`) and is drawn in the addon
preferences, so it can be rebound when it collides with something else. Design
and the verified transport/exporter facts:
`docs/adr/0007-blender-model-ingest.md`.

**This file imports no `bpy` at module scope.** Blender only needs `bl_info` plus
`register`/`unregister` to be callable, so the `bpy`-touching submodules are
imported lazily inside `register()`. That keeps `showcade_bridge.transport`,
`.exporter`, `.policy` and `.worker` importable under plain `unittest` without
Blender, which is how `task test:blender` runs (ADR 0007 decision 7).

Layout:

* `transport.py`  — the catalog HTTP client (no `bpy`)
* `exporter.py`   — the glTF exporter kwargs + RNA filter (no `bpy`)
* `policy.py`     — source resolution, warnings, result lines (no `bpy`)
* `worker.py`     — the off-thread send + its non-blocking drain (no `bpy`)
* `send.py`       — export ∘ upload; `send_bytes()` is `bpy`-free
* `export_glb.py` — `bpy.ops.export_scene.gltf` → GLB bytes (needs `bpy`)
* `props.py`      — the `ShowcadeLink` identity, stored on a Collection
* `prefs.py`      — preferences + the `Ctrl+Shift+E` keymap item
* `ops.py`        — the five operators (send, test, new/unlink, copy handle)
* `panel.py`      — `VIEW3D_PT_showcade`

Scripted use (no UI) still works, from Blender's Python console:

    from showcade_bridge.send import send_selection
    send_selection(token="dev", name="Bumper")
"""

from __future__ import annotations

#: `blender` is the FLOOR, and it is a checked claim rather than an aspiration
#: (bd showcade-e9u4): every property this addon needs — including the two
#: compression switches whose absence is fatal, `export_draco_mesh_compression_
#: enable` and `export_use_gltfpack` — exists in the 4.2 glTF exporter, verified
#: against each released exporter's source. The one that does NOT exist before
#: 5.2 is the native `export_meshopt_compression_enable`, which is why
#: `exporter.REQUIRED_KWARGS` treats it as version-gated instead of required; see
#: the version table in `exporter.py` and `DeclaredBlenderFloor` in the tests.
#: Raising or lowering this number without re-checking that table is how it goes
#: back to being a wish.
bl_info = {
    "name": "Showcade Bridge",
    "description": "Send the selection to showcade's asset catalog as a GLB",
    "author": "showcade",
    "version": (0, 1, 0),
    "blender": (4, 2, 0),
    "location": "View3D > Sidebar > Showcade",
    "category": "Import-Export",
    "doc_url": "https://github.com/caderuntime/blender-bridge",
}

#: The modules owning Blender-side classes, in REGISTRATION order — properties
#: first, because the operators and the panel reference them; unregistration
#: walks it backwards (a PointerProperty outliving its PropertyGroup crashes).
_UI_MODULES: tuple[str, ...] = ("props", "prefs", "ops", "panel")


def _ui_modules() -> list:
    import importlib

    return [importlib.import_module(f".{name}", __package__) for name in _UI_MODULES]


def register() -> None:
    for module in _ui_modules():
        module.register()


def unregister() -> None:
    for module in reversed(_ui_modules()):
        module.unregister()
