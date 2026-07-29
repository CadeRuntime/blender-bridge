# SPDX-FileCopyrightText: 2026 caderuntime <caderuntime@cade.run>
# SPDX-License-Identifier: GPL-3.0-or-later

"""policy.py — the operator's *decisions*, with no `bpy` in sight.

Everything the Send button has to work out before it touches Blender data lives
here so it can be unit-tested without Blender (ADR 0007 decision 7, the same
split that keeps `transport.py`/`exporter.py` importable): which objects the
export covers, what warnings the panel shows, and what one line the user reads
when it is over.

The one rule worth stating twice: **an empty selection never silently becomes
the whole scene.** That is how a 200 MB scene gets uploaded by accident. With
nothing selected the source falls back to the active collection, and if that is
empty too the operator *cancels with an error* — the whole scene is only ever
exported when the user picks `SCENE` on purpose.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, NamedTuple
from urllib.parse import urlencode

__all__ = [
    "HANDLE_SCHEME",
    "MAX_REASONABLE_M",
    "MIN_REASONABLE_M",
    "SOURCE_ITEMS",
    "SourceError",
    "SourcePlan",
    "bounds_max_dimension_m",
    "clipboard_handle",
    "resolve_source",
    "rotate_generations",
    "size_warning",
    "summarize_upload",
]

#: The Source dropdown, in enum-item order. `SELECTION` is the default; `SCENE`
#: exists so "export everything" stays possible, but only as a deliberate pick.
SOURCE_ITEMS: tuple[tuple[str, str, str], ...] = (
    ("SELECTION", "Selection", "Export the selected objects (falls back to the active collection)"),
    ("COLLECTION", "Active Collection", "Export the active collection and its children"),
    ("SCENE", "Whole Scene", "Export every object in the scene — check the size before sending"),
)
_SOURCE_IDS = frozenset(item[0] for item in SOURCE_ITEMS)

#: Bounds for the "this does not look like a prop" warning. A pinball device is
#: centimetres-to-decimetres; metre-scale means the .blend is in the wrong units
#: (or a light/empty crept into the selection) and sub-centimetre means the
#: browser's auto-fit will be scaling up by ~1000x.
MAX_REASONABLE_M = 5.0
MIN_REASONABLE_M = 0.01


class SourceError(RuntimeError):
    """Nothing to export. Carries the fix, not the condition."""


class SourcePlan(NamedTuple):
    """What to export, as exporter kwargs plus a line for the panel."""

    scope: str
    """The EFFECTIVE scope — ``SELECTION`` | ``COLLECTION`` | ``SCENE``. Not
    necessarily the requested source: an empty SELECTION resolves to COLLECTION,
    and the caller needs the resolved one to measure the right objects."""
    kwargs: Mapping[str, Any]
    """Overrides merged into `exporter.EXPORT_KWARGS` (RNA-filtered downstream)."""
    label: str
    """Human summary, e.g. ``"3 selected objects"``."""
    fell_back: bool
    """True when SELECTION found nothing and the active collection was used."""


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def resolve_source(
    source: str,
    *,
    selected_count: int,
    collection_name: str | None,
    collection_count: int,
    scene_count: int,
) -> SourcePlan:
    """Turn the Source enum + the scene's counts into exporter kwargs.

    Raises `SourceError` when the chosen source is empty — the operator reports
    it and cancels. Never returns a whole-scene export for an empty SELECTION.
    """
    if source not in _SOURCE_IDS:
        raise SourceError(f"unknown source {source!r} — pick one of {', '.join(sorted(_SOURCE_IDS))}")

    if source == "SCENE":
        if scene_count <= 0:
            raise SourceError("the scene is empty — there is nothing to send")
        return SourcePlan(
            "SCENE", {"use_selection": False}, _plural(scene_count, "object") + " in the scene", False
        )

    if source == "SELECTION" and selected_count > 0:
        return SourcePlan(
            "SELECTION", {"use_selection": True}, _plural(selected_count, "selected object"), False
        )

    # Either COLLECTION was picked, or SELECTION was empty and this is the
    # fallback. Both land on the active collection; only the second is a
    # fallback, and the panel says so.
    fell_back = source == "SELECTION"
    if not collection_name or collection_count <= 0:
        raise SourceError(
            "nothing selected and the active collection is empty — select the objects to send"
            if fell_back
            else "the active collection is empty — select a collection with objects in it"
        )
    return SourcePlan(
        "COLLECTION",
        # `use_selection=False` matters: leaving it True would intersect the
        # collection with an empty selection and export nothing at all.
        {"use_selection": False, "use_active_collection": True, "use_active_collection_with_nested": True},
        f"{_plural(collection_count, 'object')} in “{collection_name}”",
        fell_back,
    )


def bounds_max_dimension_m(points: Iterable[Sequence[float]], scale_length: float) -> float:
    """Longest edge of the box UNIONING every world-space point, in METRES.

    **The whole selection's box, not the biggest object's own box** (bd
    showcade-x9r7). Measuring per-object made the canonical mistake invisible:
    "I selected the entire level" is many *small* objects spread over 30 m, so
    the per-object longest edge stayed prop-sized and `size_warning` never fired
    on the exact case it exists for. The union sees the 30 m.

    `points` are world-space corners (`matrix_world @ bound_box[i]`), so an
    object's rotation and its parents' transforms are already in them.
    `scale_length` converts Blender units → metres; it is `scene.unit_settings.
    scale_length`, the very setting the panel warns about, so measuring without
    it would hide the mistake this is looking for. 0.0 means "nothing to
    measure", which `size_warning` treats as silence rather than as a claim.
    """
    low = [math.inf] * 3
    high = [-math.inf] * 3
    measured = False
    for point in points:
        coords = [float(value) for value in tuple(point)[:3]]
        if len(coords) < 3:
            continue
        measured = True
        for axis in range(3):
            low[axis] = min(low[axis], coords[axis])
            high[axis] = max(high[axis], coords[axis])
    if not measured:
        return 0.0
    longest = max(high[axis] - low[axis] for axis in range(3))
    return longest * abs(float(scale_length))


def size_warning(max_dimension_m: float) -> str | None:
    """Flag a bounding box that is not prop-shaped. Advisory: the send proceeds.

    The addon never rescales (ADR 0007 decision 2) — the browser's `fitScale()`
    owns metres→table-units — so this is the only place a units mistake in the
    .blend becomes visible before the model looks wrong in showcade.
    """
    if max_dimension_m <= 0.0:
        return None
    if max_dimension_m > MAX_REASONABLE_M:
        return (
            f"the selection is {max_dimension_m:.3g} m across — over {MAX_REASONABLE_M:g} m is "
            "scene-sized, not prop-sized; check the scene units or the selection"
        )
    if max_dimension_m < MIN_REASONABLE_M:
        return (
            f"the selection is {max_dimension_m:.3g} m across — under {MIN_REASONABLE_M:g} m; "
            "showcade will scale it up hard, so check the scene units"
        )
    return None


def rotate_generations(*, last: str, prev: str, new: str) -> tuple[str, str]:
    """Advance the two-generation window after a send. Returns ``(last, prev)``.

    **A dedupe is not a new generation.** The catalog returns the *existing* row
    for unchanged bytes under the same name, so rotating on it would push the
    still-current asset into `prev` — and the next real send would then delete
    the generation the browser is looking at, one back instead of two. Which is
    exactly the race the two-generation window exists to avoid.
    """
    if not new or new == last:
        return last, prev
    return new, last


#: The text handle `wm.clipboard` carries — a catalog REFERENCE, not bytes, so a
#: paste in showcade is instant (the blob is already stored and cached). The
#: browser-side listener is ADR 0007's "adopted: the text handle".
HANDLE_SCHEME = "showcade:model"


def clipboard_handle(*, asset_id: str, sha256: str = "", name: str = "", link_id: str = "") -> str:
    """`showcade:model?id=…&sha256=…&name=…&link=…`, percent-encoded.

    `name` is free text (spaces, quotes, `&`) and goes into a query string, so
    encoding it is not cosmetic — an unescaped `&` would truncate the handle.
    """
    if not asset_id:
        raise ValueError("a handle needs an asset id — send the model first")
    fields = [("id", asset_id), ("sha256", sha256), ("name", name), ("link", link_id)]
    query = urlencode([(key, value) for key, value in fields if value])
    return f"{HANDLE_SCHEME}?{query}"


def summarize_upload(name: str, *, status: int, sent_bytes: int, asset_id: str) -> str:
    """The one-line result the panel keeps until the next send."""
    where = f" [{asset_id}]" if asset_id else ""
    if status == 200:
        # Dedupe is (sha256, name): unchanged bytes under the same name write
        # nothing at all, which is a success, not a no-op to apologise for.
        return f"“{name}” unchanged — the catalog already had it{where}"
    if sent_bytes <= 0:
        return f"“{name}” added — the blob was already stored, metadata only{where}"
    return f"“{name}” sent — {sent_bytes / (1024 * 1024):.2f} MB uploaded{where}"
