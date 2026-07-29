# SPDX-FileCopyrightText: 2026 caderuntime <caderuntime@cade.run>
# SPDX-License-Identifier: GPL-3.0-or-later

"""fake_bpy.py — a `bpy` stand-in, just big enough to IMPORT the addon's UI half.

`task test:blender` must run without Blender (ADR 0007 decision 7), which is why
`transport.py` / `exporter.py` / `policy.py` / `worker.py` import no `bpy` at all
— **and that stays true**. But two rules do live in the `bpy` half and are worth
pinning anyway:

* `props.py` must clear the connection status when a .blend is loaded
  (bd showcade-r4gx) — a registration fact, so the test has to reach `register()`;
* `ops.max_dimension_m` must measure the WHOLE selection (bd showcade-x9r7) —
  arithmetic over `bound_box` × `matrix_world`, which needs no Blender at all.

Neither needs a running Blender, only the handful of names the modules touch at
import time. This is a test-only shim: it is never imported by the addon, and it
is removed from `sys.modules` again on the way out so no other test can pick it
up by accident.
"""

from __future__ import annotations

import contextlib
import sys
import types

#: Addon modules that import `bpy`; purged after the shim comes back out so a
#: later test cannot end up holding a fake-bpy-backed module.
_BPY_MODULES = (
    "showcade_bridge.props",
    "showcade_bridge.prefs",
    "showcade_bridge.ops",
    "showcade_bridge.panel",
    "showcade_bridge.export_glb",
)


def _property(*_args, **_kwargs):
    """Every `bpy.props.*` factory. The addon annotates with these; Blender turns
    them into real properties at registration and nothing here needs to."""
    return


def build_module() -> types.ModuleType:
    """A module object that answers everything the addon touches at import time."""
    bpy = types.ModuleType("bpy")

    class PropertyGroup:
        pass

    class Operator:
        def report(self, *_args, **_kwargs):  # pragma: no cover - operators are not run here
            return None

    class Panel:
        pass

    class AddonPreferences:
        pass

    class Collection:
        pass

    class Scene:
        pass

    bpy.types = types.SimpleNamespace(
        PropertyGroup=PropertyGroup,
        Operator=Operator,
        Panel=Panel,
        AddonPreferences=AddonPreferences,
        Collection=Collection,
        Scene=Scene,
    )
    bpy.props = types.SimpleNamespace(
        StringProperty=_property,
        EnumProperty=_property,
        BoolProperty=_property,
        FloatProperty=_property,
        IntProperty=_property,
        PointerProperty=_property,
    )

    registered: list = []
    bpy.utils = types.SimpleNamespace(
        register_class=registered.append,
        unregister_class=lambda cls: registered.remove(cls) if cls in registered else None,
    )
    bpy.registered_classes = registered  # for assertions

    def persistent(function):
        """Blender's decorator marks a handler as surviving a file load; the shim
        records that it was applied, so a test can tell a `@persistent` handler
        from a plain one (a plain one is dropped by the very load it reacts to)."""
        function._fake_bpy_persistent = True
        return function

    bpy.app = types.SimpleNamespace(
        handlers=types.SimpleNamespace(persistent=persistent, load_post=[], save_post=[])
    )
    bpy.data = types.SimpleNamespace(scenes=[])
    bpy.context = types.SimpleNamespace()
    bpy.ops = types.SimpleNamespace()
    return bpy


@contextlib.contextmanager
def installed():
    """Install the shim as `bpy` for the duration of the block."""
    previous = sys.modules.get("bpy")
    stale = {name: sys.modules.pop(name) for name in _BPY_MODULES if name in sys.modules}
    fake = build_module()
    sys.modules["bpy"] = fake
    try:
        yield fake
    finally:
        for name in _BPY_MODULES:
            sys.modules.pop(name, None)
        sys.modules.update(stale)
        if previous is None:
            sys.modules.pop("bpy", None)
        else:  # pragma: no cover - only when a real bpy is importable
            sys.modules["bpy"] = previous


class FakeScene:
    """A scene whose `showcade` property group only carries the status line."""

    def __init__(self, status: str = ""):
        self.showcade = types.SimpleNamespace(status=status)


class FakeObject:
    """An object with the two attributes the size warning measures.

    `bound_box` is LOCAL (Blender's is) and `matrix_world` places it — the pair
    is what makes "the selection spans 30 m" measurable at all. `dimensions` is
    carried too, and deliberately: it is what the per-object measurement used,
    so a test built on these fakes exercises the difference rather than an
    absence.
    """

    def __init__(
        self,
        *,
        size: float = 0.1,
        location=(0.0, 0.0, 0.0),
        scale: float = 1.0,
        name: str = "Obj",
        type: str = "MESH",
    ):
        # `type` and `name` are not decoration: a LIGHT/CAMERA has a zero-size
        # bound_box but a real world position, and the exporter drops both
        # (export_lights/export_cameras are False), so a fake that cannot BE one
        # cannot catch a union AABB dragged out to the default scene's camera.
        self.name = name
        self.type = type
        half = size / 2.0
        self.bound_box = tuple(
            (x * half, y * half, z * half)
            for x in (-1.0, 1.0)
            for y in (-1.0, 1.0)
            for z in (-1.0, 1.0)
        )
        self.dimensions = (size * scale, size * scale, size * scale)
        self.matrix_world = [
            [scale, 0.0, 0.0, float(location[0])],
            [0.0, scale, 0.0, float(location[1])],
            [0.0, 0.0, scale, float(location[2])],
            [0.0, 0.0, 0.0, 1.0],
        ]
