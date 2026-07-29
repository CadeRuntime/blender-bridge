# SPDX-FileCopyrightText: 2026 caderuntime <caderuntime@cade.run>
# SPDX-License-Identifier: GPL-3.0-or-later

"""panel.py — the N-sidebar panel (View3D ▸ Sidebar ▸ Showcade).

Draw code only: every value it shows comes from `props.py`, and every decision
it renders (which source, what warning) comes from the `bpy`-free `policy.py`.
`draw()` must stay cheap and side-effect-free — it runs on every redraw, so it
never mints a link id, never writes a property and above all never touches the
network.

The warnings are the reason the panel earns its space. A wrong
`unit_settings.scale_length`, or a selection that is scene-sized rather than
prop-sized, produces a GLB that *uploads fine* and only looks wrong later in
showcade; saying so here is the cheapest place to catch it.
"""

from __future__ import annotations

import bpy  # type: ignore[import-not-found]  # provided by Blender

from . import exporter, ops, policy, prefs, transport

__all__ = ["VIEW3D_PT_showcade", "register", "unregister"]


class VIEW3D_PT_showcade(bpy.types.Panel):
    bl_idname = "VIEW3D_PT_showcade"
    bl_label = "Showcade"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Showcade"

    @classmethod
    def poll(cls, context) -> bool:
        return context.mode == "OBJECT"

    def draw(self, context) -> None:
        layout = self.layout
        settings = prefs.get_prefs(context)
        scene_props = context.scene.showcade
        collection = context.collection

        # ── connection ──
        connection = layout.box()
        row = connection.row(align=True)
        row.operator("showcade.test_connection", icon="LINKED")
        if settings is not None:
            connection.label(text=settings.endpoint, icon="URL")
        if scene_props.status:
            connection.label(text=scene_props.status, icon="INFO")

        if collection is None:  # pragma: no cover - there is always one in practice
            layout.label(text="no active collection", icon="ERROR")
            return
        link = collection.showcade

        # ── what gets sent ──
        layout.prop(scene_props, "source")
        layout.prop(link, "asset_name", text="Name")
        layout.prop(link, "device")

        # ── identity ──
        identity = layout.box()
        row = identity.row(align=True)
        if link.link_id:
            row.label(text=transport.link_tag(link.link_id), icon="LINKED")
            row.operator("showcade.unlink", text="", icon="UNLINKED")
        else:
            row.label(text="not linked — the first send mints an id", icon="UNLINKED")
        row.operator("showcade.new_link", text="", icon="FILE_REFRESH")

        # ── send ──
        layout.operator("showcade.send", icon="EXPORT")
        if link.last_result:
            layout.label(text=link.last_result, icon="CHECKMARK")
        if link.last_asset_id:
            layout.operator("showcade.copy_handle", icon="COPYDOWN")

        _draw_warnings(layout, context)


def _draw_warnings(layout, context) -> None:
    """The two silent-failure warnings, and the resolved-source line."""
    scene = context.scene
    scale_warning = exporter.scale_length_warning(scene.unit_settings.scale_length)
    if scale_warning:
        _wrapped(layout, scale_warning, icon="ERROR")

    try:
        plan = policy.resolve_source(
            scene.showcade.source,
            selected_count=len(context.selected_objects or ()),
            collection_name=context.collection.name if context.collection else None,
            collection_count=len(context.collection.all_objects) if context.collection else 0,
            scene_count=len(scene.objects),
        )
    except policy.SourceError as exc:
        _wrapped(layout, str(exc), icon="ERROR")
        return

    layout.label(text=plan.label, icon="OUTLINER_OB_MESH" if not plan.fell_back else "INFO")

    size_warning = policy.size_warning(
        ops.max_dimension_m(ops.source_objects(context, plan.scope), scene.unit_settings.scale_length)
    )
    if size_warning:
        _wrapped(layout, size_warning, icon="ERROR")


def _wrapped(layout, text: str, *, icon: str = "NONE", width: int = 46) -> None:
    """Blender labels do not wrap; a long warning would just be cut off."""
    column = layout.column(align=True)
    line: list[str] = []
    for word in text.split():
        if line and sum(len(part) + 1 for part in line) + len(word) > width:
            column.label(text=" ".join(line), icon=icon)
            icon = "BLANK1"
            line = []
        line.append(word)
    if line:
        column.label(text=" ".join(line), icon=icon)


def register() -> None:
    bpy.utils.register_class(VIEW3D_PT_showcade)


def unregister() -> None:
    bpy.utils.unregister_class(VIEW3D_PT_showcade)
