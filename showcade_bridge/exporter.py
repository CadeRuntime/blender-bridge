# SPDX-FileCopyrightText: 2026 caderuntime <caderuntime@cade.run>
# SPDX-License-Identifier: GPL-3.0-or-later

"""exporter.py — the glTF exporter settings, and the RNA filter that applies them.

**No `bpy` here either** (see `transport.py`): the kwargs and the filter are the
part worth unit-testing, and they are pure data. `export_glb.py` is the thin
`bpy`-touching wrapper that feeds `bpy.ops.export_scene.gltf`.

Two rules this module exists to enforce:

**1. Never call the operator with a literal dict.** Property identifiers drift
across Blender versions and an unknown kwarg is a hard `TypeError`, so the
desired dict is intersected with the operator's live RNA property names and
whatever was dropped is reported (`resolve_export_kwargs`). All of the names
below were enumerated against the installed **Blender 5.2.0 LTS** exporter's 111
properties; the filter is version-drift insurance, not a workaround.

That drift is not hypothetical, and it is what `bl_info`'s 4.2 floor rests on
(bd showcade-e9u4). Checked against the *released* exporter source for each
version, the native meshopt switch is **5.2 and newer only**:

| Blender | draco | gltfpack | native meshopt |
|---------|-------|----------|----------------|
| 4.2 · 4.3 · 4.5 · 5.0 · 5.1 | yes | yes | **no such property — and no meshopt path at all** |
| 5.2 | yes | yes | yes |

So requiring `export_meshopt_compression_enable` outright would hard-refuse
every 4.2–5.1 exporter — versions `bl_info` and `blender_manifest.toml`
advertise — over a compression path those exporters cannot even perform. Hence
the split below: a switch whose *feature* exists everywhere we support must be
present (its absence means we cannot prove compression is off), while the
5.2-only one is forced False where it exists and merely reported where it does
not.

**2. Every compression path must be off.** The showcade browser loader
deliberately registers no decoder-backed extensions (the strict-CSP embed cannot
CDN-fetch a decoder), so a compressed GLB is one showcade *silently fails to
load*. Blender 5.2 has **three** such switches — Draco, a NATIVE meshopt path,
and the external `gltfpack` — and all three are disabled here, and re-disabled by
`resolve_export_kwargs` *after* it merges a caller's overrides, so no override can
turn one back on.

**And one rule about what is NOT here: no scale.** The export is true Blender
metres. glTF's unit *is* the metre, and only the browser knows the target device
footprint, so `fitScale()` owns metres→table-units (ADR 0007 decision 2). There
is deliberately no scale/unit kwarg in `EXPORT_KWARGS`, and a test asserts that.
"""

from __future__ import annotations

import logging
import os
import re
import string
from collections.abc import Iterable, Mapping
from typing import Any

__all__ = [
    "COMPRESSION_SWITCHES",
    "EXPORT_KWARGS",
    "REQUIRED_KWARGS",
    "UNIVERSAL_COMPRESSION_SWITCHES",
    "VERSIONED_COMPRESSION_SWITCHES",
    "clamped_compression",
    "resolve_export_kwargs",
    "safe_stem",
    "scale_length_warning",
]

log = logging.getLogger(__name__)

#: Charset a temp-file stem is squashed to. Deliberately tiny — the stem is
#: cosmetic, so nothing is gained by preserving exotic characters.
_SAFE_STEM_CHARS = frozenset(string.ascii_letters + string.digits + "-_")
_STEM_FALLBACK = "showcade"


def safe_stem(basename: str) -> str:
    """A filesystem-inert filename stem: no separators, no traversal, never empty.

    The asset NAME reaches the exporter as free text — `showcade-q42r.5` wires it
    straight to a UI preference, and the service only bounds it to 1..120
    non-blank chars, where slashes and dots are perfectly legal. Interpolating
    that into a path is a traversal: ``"../escaped"`` writes OUTSIDE the
    self-deleting temp dir and *survives* its cleanup, and an ABSOLUTE name is
    worse still, because ``os.path.join`` discards everything before it and the
    exporter writes at that path verbatim.

    Squashing rather than rejecting is safe because the stem is cosmetic: the
    catalog keys off ``meta.name``, and the multipart filename is built
    separately by ``transport._header_safe``.
    """
    # basename() first, so "a/b" collapses to "b" rather than "a_b"; the charset
    # squash then neutralises "..", drive letters, NULs and anything exotic.
    squashed = "".join(
        ch if ch in _SAFE_STEM_CHARS else "_" for ch in os.path.basename(basename)
    )
    return re.sub(r"_+", "_", squashed).strip("_")[:64] or _STEM_FALLBACK

#: Compression switches every exporter the addon supports HAS (both since well
#: before 4.2). A missing one means this Blender's exporter is not the one we
#: reasoned about, and we cannot prove compression is off — so it fails the
#: export rather than shipping a maybe-compressed GLB.
UNIVERSAL_COMPRESSION_SWITCHES: frozenset[str] = frozenset(
    {
        # KHR_draco_mesh_compression — not registered in modelLoader.ts
        "export_draco_mesh_compression_enable",
        # invokes the external gltfpack, which re-encodes to meshopt
        "export_use_gltfpack",
    }
)

#: Compression switches only NEWER exporters have (see the version table above).
#: Present ⇒ forced False like any other; absent ⇒ that exporter has no such
#: compression path to disable, so the export is safe and its absence is merely
#: reported in `dropped` (bd showcade-e9u4).
VERSIONED_COMPRESSION_SWITCHES: frozenset[str] = frozenset(
    {
        # EXT_meshopt_compression — not registered, and NATIVE from Blender 5.2
        # (it is not a gltfpack post-process, which is the easy thing to get wrong)
        "export_meshopt_compression_enable",
    }
)

#: Switches that would produce a GLB showcade cannot load. Every one of these
#: MUST be present in EXPORT_KWARGS and MUST be False — and `resolve_export_kwargs`
#: forces them back to False after merging overrides, so this is an invariant of
#: the resolved kwargs, not just of the defaults.
COMPRESSION_SWITCHES: frozenset[str] = UNIVERSAL_COMPRESSION_SWITCHES | VERSIONED_COMPRESSION_SWITCHES

#: Without these the upload is wrong rather than merely suboptimal, so a drop
#: here fails the export instead of being logged.
REQUIRED_KWARGS: frozenset[str] = frozenset({"filepath", "export_format"}) | UNIVERSAL_COMPRESSION_SWITCHES

EXPORT_KWARGS: Mapping[str, Any] = {
    # One self-contained binary file — the catalog stores a single blob.
    "export_format": "GLB",
    # The exporter does the Z-up→Y-up conversion. Do NOT pre-rotate the scene.
    "export_yup": True,
    # Bake modifiers, so what the user sees is what showcade loads.
    "export_apply": True,
    # AUTO keeps PNG/JPEG as authored; never WEBP (see below).
    "export_image_format": "AUTO",
    # A static prop: no clips, no scene furniture.
    "export_animations": False,
    "export_lights": False,
    "export_cameras": False,
    # Tangents inflate the GLB and Babylon derives them when a normal map needs them.
    "export_tangents": False,
    # EXT_texture_webp IS registered, but the option emits a PNG fallback TOO,
    # inflating the GLB against the 32 MB cap for no gain.
    "export_image_add_webp": False,
    "export_image_webp_fallback": False,
    # ── the three compression paths (see COMPRESSION_SWITCHES) ──
    "export_draco_mesh_compression_enable": False,
    "export_meshopt_compression_enable": False,
    "export_use_gltfpack": False,
}


def clamped_compression(overrides: Mapping[str, Any]) -> list[str]:
    """The COMPRESSION_SWITCHES an override tried to turn ON, sorted.

    Clamping is not an error — the resulting GLB is correct either way — but it
    must not be *silent*, which is this module's own stated rule for a setting
    that does not take effect (see `resolve_export_kwargs`): a caller that names
    a compression switch used to get no signal at all, neither in ``dropped``
    (that channel means "absent from the RNA") nor in the log (bd showcade-py7c).
    """
    return sorted(name for name, value in overrides.items() if name in COMPRESSION_SWITCHES and bool(value))


def resolve_export_kwargs(
    available: Iterable[str],
    **overrides: Any,
) -> tuple[dict[str, Any], list[str]]:
    """Intersect the desired kwargs with the operator's live RNA properties.

    ``available`` is ``bpy.ops.export_scene.gltf.get_rna_type().properties.keys()``
    (passed in, so this stays `bpy`-free). Returns ``(kwargs, dropped)``; the
    caller logs ``dropped`` — a silently dropped setting is the one failure mode
    the RNA filter cannot prevent, so it must be visible.

    Raises ``ValueError`` when a REQUIRED_KWARGS name is missing from the RNA:
    exporting without one of those is worse than not exporting. A
    VERSIONED_COMPRESSION_SWITCHES name is deliberately NOT required — an
    exporter without it has no such compression path (bd showcade-e9u4) — so it
    lands in ``dropped`` and is logged like any other absent property.

    ``overrides`` may tune anything EXCEPT a COMPRESSION_SWITCHES entry: those
    are re-applied as ``False`` *after* the merge, so rule 2 above holds by
    construction rather than by everyone-remembers. (Before this, a caller
    passing ``export_draco_mesh_compression_enable=True`` got it back as True,
    and REQUIRED_KWARGS only ever guarded the switch's RNA *presence*, never its
    value — see bd showcade-0yyi.) A clamp is LOGGED, on the same channel a drop
    is, so the refusal is as visible as a drop (`clamped_compression`, bd
    showcade-py7c).
    """
    desired: dict[str, Any] = {**EXPORT_KWARGS, **overrides}
    # Enforced here, not merely documented: no override can re-enable a
    # compression path and ship a GLB showcade silently fails to load.
    desired.update(dict.fromkeys(COMPRESSION_SWITCHES, False))
    clamped = clamped_compression(overrides)
    if clamped:
        log.warning(
            "showcade: refused to enable compression (%s) — showcade's loader registers no "
            "decoder-backed extension, so a compressed GLB would silently fail to load; "
            "exporting uncompressed",
            ", ".join(clamped),
        )
    names = set(available)
    kwargs = {key: value for key, value in desired.items() if key in names}
    dropped = sorted(key for key in desired if key not in names)
    missing = sorted(REQUIRED_KWARGS & set(dropped))
    if missing:
        raise ValueError(
            "the glTF exporter is missing load-bearing properties "
            f"({', '.join(missing)}) — this Blender's exporter is not supported"
        )
    return kwargs, dropped


def scale_length_warning(scale_length: float) -> str | None:
    """A panel warning, not a correction: the addon never rescales.

    ``scene.unit_settings.scale_length != 1.0`` means the .blend's metre is not
    a metre, so the exported GLB's units are off by that factor and showcade's
    auto-fit starts from the wrong size.
    """
    if abs(scale_length - 1.0) < 1e-9:
        return None
    return (
        f"scene.unit_settings.scale_length is {scale_length:g}, not 1.0 — the GLB is exported "
        "in metres as-authored and showcade will not compensate; set it to 1.0 for a 1:1 export"
    )
