# SPDX-FileCopyrightText: 2026 caderuntime <caderuntime@cade.run>
# SPDX-License-Identifier: GPL-3.0-or-later

"""prefs.py — addon preferences and the Ctrl+Shift+E keymap item.

Two things that have to live together: the hotkey is registered here **and drawn
in the preferences**, because a keymap collision is not a hypothetical — every
addon competes for the same modifier space — and an unrebindable shortcut is a
support ticket. `rna_keymap_ui.draw_kmi` gives the stock rebind widget, so a
user's change lands in their user keyconfig and persists like any other.

`Ctrl+Shift+E` in the **Object Mode** keymap is free: Blender's `Ctrl+E` is the
edit-mode edge menu, which this never sees.

Defaults point at `task dev:assets` (`http://localhost:8787/api/assets`, token
`dev`) because that is what a fresh install can talk to with no setup — not
because it is the only thing it can ever talk to. The hosted product
(`https://show.cade.run/api/assets`) is an ordinary target, and the normal way to
reach it is **Sign in**: the device grant fills in `session_token`, which takes
precedence over the upload token from there on. What a hosted deployment has no
use for is the shared *token* — it ships none and answers a token-only write with
`403 catalog is read-only` — so it is that credential, not the endpoint, that is
the misconfiguration. (ADR 0007 predates the device grant and calls the endpoint
itself the mistake; `bd showcade-fmq8` is where the two credentials were reconciled.)
"""

from __future__ import annotations

import bpy  # type: ignore[import-not-found]  # provided by Blender

from . import deviceauth, transport

__all__ = ["ShowcadePreferences", "get_prefs", "register", "unregister"]

#: (keymap, keymap_item) pairs this addon owns; removed verbatim on unregister
#: so a disable/enable cycle cannot leave a duplicate binding behind.
_keymaps: list[tuple] = []

_SEND_OPERATOR = "showcade.send"
_KEYMAP_NAME = "Object Mode"


class ShowcadePreferences(bpy.types.AddonPreferences):
    bl_idname = __package__

    endpoint: bpy.props.StringProperty(
        name="Endpoint",
        description="The asset catalog's base URL — `task dev:assets` serves it on port 8787",
        default=transport.DEFAULT_ENDPOINT,
    )
    token: bpy.props.StringProperty(
        name="Upload token",
        description="Bearer token for writes (ASSETS_UPLOAD_TOKEN); `task dev:assets` uses 'dev'",
        default="dev",
        subtype="PASSWORD",
    )
    auth_endpoint: bpy.props.StringProperty(
        name="Auth endpoint",
        description=(
            "Better Auth base URL, for signing in to a deployment that verifies SESSIONS "
            "instead of a shared token. `task dev:auth` serves it on port 8787"
        ),
        default=deviceauth.DEFAULT_AUTH_ENDPOINT,
    )
    session_token: bpy.props.StringProperty(
        name="Session",
        description=(
            "Filled in by Sign in — a Better Auth session token from the device grant. "
            "Takes precedence over the upload token, so a signed-in send is attributed to "
            "YOU (owner-scoped listing, per-user quota) instead of being anonymous"
        ),
        default="",
        subtype="PASSWORD",
    )
    timeout_s: bpy.props.FloatProperty(
        name="Timeout (s)",
        description="Per-request timeout; the send runs on a worker thread, so this never freezes the UI",
        default=transport.DEFAULT_TIMEOUT_S,
        min=1.0,
        max=600.0,
    )
    gc_previous: bpy.props.BoolProperty(
        name="Delete old uploads",
        description=(
            "After a successful send, delete the asset TWO generations back. Twenty iterations of a "
            "6 MB model is 120 MB of dead blobs and nothing else reclaims them; keeping two "
            "generations makes the race with a browser that is still loading the old one benign"
        ),
        default=True,
    )
    insecure_tls: bpy.props.BoolProperty(
        name="Skip TLS verification",
        description=(
            "For an HTTPS endpoint whose certificate your machine does not trust — "
            "a private or self-hosted deployment. "
            "Leave off for localhost — plain HTTP does not need it"
        ),
        default=False,
    )
    auto_send_on_save: bpy.props.BoolProperty(
        name="Send on save",
        description=(
            "Re-send the active collection every time the .blend is saved. Only fires for a collection "
            "that already has a link id and a name, so saving an unrelated file never uploads anything"
        ),
        default=False,
    )

    def draw(self, context) -> None:
        layout = self.layout

        catalog = layout.box()
        catalog.label(text="Catalog", icon="URL")
        catalog.prop(self, "endpoint")
        catalog.prop(self, "token")
        row = catalog.row(align=True)
        row.prop(self, "timeout_s")
        row.prop(self, "insecure_tls")
        catalog.operator("showcade.test_connection", icon="LINKED")

        # Sign-in is its own box: it targets the AUTH service, not the catalog, and the two
        # can legitimately live on different origins.
        account = layout.box()
        account.label(text="Account", icon="USER")
        account.prop(self, "auth_endpoint")
        row = account.row(align=True)
        if self.session_token:
            # Never render the token itself, even masked — there is no reason to put a
            # credential on screen, and "signed in" is the only fact a user needs.
            row.label(text="Signed in — sends are attributed to you", icon="CHECKMARK")
            row.operator("showcade.sign_out", icon="X", text="Sign out")
        else:
            row.label(text="Not signed in — sends use the upload token", icon="INFO")
            row.operator("showcade.sign_in", icon="URL", text="Sign in")

        behaviour = layout.box()
        behaviour.label(text="Behaviour", icon="PREFERENCES")
        behaviour.prop(self, "gc_previous")
        behaviour.prop(self, "auto_send_on_save")

        keys = layout.box()
        keys.label(text="Keymap", icon="KEYINGSET")
        _draw_keymap(keys, context)


def _draw_keymap(layout, context) -> None:
    """The stock rebind widget, against the USER keyconfig so edits persist."""
    import rna_keymap_ui  # type: ignore[import-not-found]  # Blender's own UI helper

    keyconfig = context.window_manager.keyconfigs.user
    keymap = keyconfig.keymaps.get(_KEYMAP_NAME) if keyconfig else None
    if keymap is None:  # pragma: no cover - only in a keyconfig-less session
        layout.label(text=f"no '{_KEYMAP_NAME}' keymap in this configuration", icon="ERROR")
        return
    drawn = 0
    for item in keymap.keymap_items:
        if item.idname == _SEND_OPERATOR:
            rna_keymap_ui.draw_kmi([], keyconfig, keymap, item, layout, 0)
            drawn += 1
    if not drawn:
        # A user can delete the item outright; say so rather than drawing nothing.
        layout.label(text="the Send shortcut has been removed — add it back here", icon="ERROR")


def get_prefs(context):
    """This addon's preferences, or None when it is running unregistered.

    `__package__` is the addon id under both the legacy addon path and the 4.2+
    extension path (`bl_ext.<repo>.showcade_bridge`), so this one lookup covers
    both installs.
    """
    addon = context.preferences.addons.get(__package__)
    return addon.preferences if addon else None


def register() -> None:
    bpy.utils.register_class(ShowcadePreferences)
    keyconfig = bpy.context.window_manager.keyconfigs.addon
    if keyconfig is None:  # `--background` has no addon keyconfig; that is fine
        return
    keymap = keyconfig.keymaps.new(name=_KEYMAP_NAME, space_type="EMPTY")
    item = keymap.keymap_items.new(_SEND_OPERATOR, "E", "PRESS", ctrl=True, shift=True)
    _keymaps.append((keymap, item))


def unregister() -> None:
    for keymap, item in _keymaps:
        keymap.keymap_items.remove(item)
    _keymaps.clear()
    bpy.utils.unregister_class(ShowcadePreferences)
