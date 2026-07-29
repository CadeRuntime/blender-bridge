# SPDX-FileCopyrightText: 2026 caderuntime <caderuntime@cade.run>
# SPDX-License-Identifier: GPL-3.0-or-later

"""export_glb.py — run Blender's glTF exporter and hand back the GLB bytes.

The only module in the addon that touches `bpy` data, and the only one that must
run on the main thread. `bpy.ops.export_scene.gltf` has **no in-memory API**, so
the export goes into a `tempfile.TemporaryDirectory()` that self-deletes; the
bytes are read back and everything after this point (see `transport.py`) is
plain data that a worker thread can carry without touching `bpy`.

Not `bpy.app.tempdir`: Blender's own copy buffer lives near there.
"""

from __future__ import annotations

import logging
import os
import tempfile
from collections.abc import Mapping
from typing import Any

import bpy  # type: ignore[import-not-found]  # provided by Blender

from . import exporter

log = logging.getLogger(__name__)

__all__ = ["export_glb_bytes", "exporter_property_names"]


def exporter_property_names() -> set[str]:
    """The live RNA property names of the glTF export operator."""
    return set(bpy.ops.export_scene.gltf.get_rna_type().properties.keys())


def export_glb_bytes(
    *,
    use_selection: bool = True,
    basename: str = "showcade",
    source_kwargs: Mapping[str, Any] | None = None,
) -> bytes:
    """Export the current selection (or the whole scene) to GLB, in memory.

    ``source_kwargs`` is `policy.SourcePlan.kwargs` — the "what to export" half
    (``use_selection`` / ``use_active_collection`` …), passed as data so the
    decision stays in the `bpy`-free `policy` module. It wins over
    ``use_selection``, which stays for the scripted call in the README.

    Returns the raw GLB bytes. Raises ``RuntimeError`` when the operator did not
    finish or wrote nothing.
    """
    with tempfile.TemporaryDirectory(prefix="showcade-glb-") as tmpdir:
        path = os.path.join(tmpdir, f"{exporter.safe_stem(basename)}.glb")
        # One dict, not two keyword args: `source_kwargs` may itself carry
        # `use_selection`, and duplicating a keyword is a TypeError.
        overrides: dict[str, Any] = {"use_selection": use_selection, **dict(source_kwargs or {})}
        kwargs, dropped = exporter.resolve_export_kwargs(
            exporter_property_names(), filepath=path, **overrides
        )
        if dropped:
            # The RNA filter contains the damage but cannot prevent a silently
            # dropped setting — this line is the only warning.
            log.warning("showcade: glTF exporter dropped unknown kwargs: %s", ", ".join(dropped))
        result = bpy.ops.export_scene.gltf(**kwargs)
        if "FINISHED" not in set(result):
            raise RuntimeError(f"glTF export did not finish ({', '.join(sorted(result))})")
        if not os.path.exists(path):
            raise RuntimeError("glTF export reported success but wrote no file")
        with open(path, "rb") as handle:
            data = handle.read()
    if not data.startswith(b"glTF"):
        raise RuntimeError("the exported file is not a GLB (missing the glTF magic)")
    return data
