# SPDX-FileCopyrightText: 2026 caderuntime <caderuntime@cade.run>
# SPDX-License-Identifier: GPL-3.0-or-later

"""props.py — where a showcade asset's identity lives inside the .blend.

**On `bpy.types.Collection`, not the scene and not the object.** A collection is
Blender's natural group-of-objects, it round-trips through save/reopen inside the
.blend for free, and one collection = one showcade asset means a .blend with four
props works on day one without a second concept.

The identity itself is the `blender:<16-hex>` link id (ADR 0007 decision 1): the
catalog tag that survives a re-export changing the sha, which is how showcade's
hot-reload finds the *current* bytes of a model you have already bound.

`bpy.types.Scene.showcade` holds the per-session defaults (which source the next
send uses, and the last-send line the panel echoes).
"""

from __future__ import annotations

import bpy  # type: ignore[import-not-found]  # provided by Blender

from . import policy, transport

__all__ = [
    "ShowcadeLink",
    "ShowcadeSceneProps",
    "active_link",
    "clear_status",
    "register",
    "unregister",
]


class ShowcadeLink(bpy.types.PropertyGroup):
    """One showcade asset, stored on the collection that produces it."""

    link_id: bpy.props.StringProperty(
        name="Link",
        description="Stable identity stamped on every upload as the blender:<id> tag",
        default="",
    )
    asset_name: bpy.props.StringProperty(
        name="Name",
        description=(
            "Catalog name. KEEP IT STABLE across re-sends — dedupe is (sha256, name), so an "
            "unchanged re-export then costs one request and writes nothing"
        ),
        default="",
        maxlen=transport.MAX_NAME_LEN,
    )
    device: bpy.props.StringProperty(
        name="Device",
        description="Optional: the showcade device this is meant for, used to prefill the bind prompt",
        default="",
    )
    # Two generations, not one: deleting the immediately-previous asset races
    # the browser's re-point, whereas keeping two makes the race benign.
    last_asset_id: bpy.props.StringProperty(default="", options={"HIDDEN"})
    prev_asset_id: bpy.props.StringProperty(default="", options={"HIDDEN"})
    #: sha of the last upload — only used to fill in the clipboard handle.
    last_sha256: bpy.props.StringProperty(default="", options={"HIDDEN"})
    last_result: bpy.props.StringProperty(default="", options={"HIDDEN"})


class ShowcadeSceneProps(bpy.types.PropertyGroup):
    """Per-scene send defaults."""

    source: bpy.props.EnumProperty(
        name="Source",
        description="What the export covers",
        items=[(ident, label, desc) for ident, label, desc in policy.SOURCE_ITEMS],
        default="SELECTION",
    )
    status: bpy.props.StringProperty(default="", options={"HIDDEN"})
    """The last connection-test line. Per-SESSION: `_clear_status_on_load` blanks
    it whenever a .blend is loaded, because a property on a PropertyGroup does
    round-trip through the file and a restored "connected — token accepted" from
    last week is a confidently wrong claim about right now (bd showcade-r4gx)."""


def active_link(context) -> ShowcadeLink | None:
    """The link of the active collection, or None when there is no collection.

    Note it does NOT create one: an unlinked collection is a perfectly normal
    state (the first send mints the id), and a property-group access must never
    write to the .blend as a side effect of drawing a panel.
    """
    collection = getattr(context, "collection", None)
    return getattr(collection, "showcade", None) if collection is not None else None


def clear_status(scenes) -> int:
    """Blank every scene's connection status. Returns how many were cleared.

    Split out of the handler (and `bpy`-free by construction — it only touches
    the objects it is handed) so the "a loaded .blend never shows a stale
    connection" rule is testable.
    """
    cleared = 0
    for scene in scenes:
        scene_props = getattr(scene, "showcade", None)
        if scene_props is not None and getattr(scene_props, "status", ""):
            scene_props.status = ""
            cleared += 1
    return cleared


@bpy.app.handlers.persistent
def _clear_status_on_load(*_args) -> None:
    """The connection status is per-SESSION — enforce that on load.

    `ShowcadeSceneProps.status` is an ordinary StringProperty on a PropertyGroup,
    so it round-trips through the .blend like every other property: the old
    "cleared on load" comment was a claim nothing implemented, and reopening a
    file showed the PREVIOUS session's "connected to http://stale:8899 — token
    accepted" as though it had just been probed (bd showcade-r4gx).

    `@persistent` is load-bearing: a plain handler is dropped by the very file
    load it exists to react to, so it would clear the status exactly once.
    """
    clear_status(bpy.data.scenes)


_CLASSES = (ShowcadeLink, ShowcadeSceneProps)


def register() -> None:
    for cls in _CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.Collection.showcade = bpy.props.PointerProperty(type=ShowcadeLink)
    bpy.types.Scene.showcade = bpy.props.PointerProperty(type=ShowcadeSceneProps)
    if _clear_status_on_load not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(_clear_status_on_load)


def unregister() -> None:
    if _clear_status_on_load in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_clear_status_on_load)
    del bpy.types.Scene.showcade
    del bpy.types.Collection.showcade
    for cls in reversed(_CLASSES):
        bpy.utils.unregister_class(cls)
