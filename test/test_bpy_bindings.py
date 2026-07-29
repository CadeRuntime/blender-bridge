# SPDX-FileCopyrightText: 2026 caderuntime <caderuntime@cade.run>
# SPDX-License-Identifier: GPL-3.0-or-later

"""The two rules that live in the `bpy` half but need no Blender to check.

`fake_bpy` supplies the names `props.py` and `ops.py` touch at import time, which
is enough to assert:

* **a loaded .blend never shows a stale connection status** (bd showcade-r4gx) —
  the status is an ordinary StringProperty and DOES round-trip through the file,
  so it takes a `load_post` handler to make the old "cleared on load" comment
  true;
* **the size warning measures the whole selection** (bd showcade-x9r7) — many
  small objects spread over 30 m is the "I selected the entire level" case the
  >5 m warning exists for, and a per-object measurement never saw it.

The addon's `bpy`-free modules are still imported plainly, without the shim; this
file does not change that split.
"""

from __future__ import annotations

import importlib
import unittest

import fake_assets  # noqa: F401  (puts the repo root on sys.path)
import fake_bpy

from showcade_bridge import policy


class ConnectionStatusIsPerSession(unittest.TestCase):
    """bd showcade-r4gx — the panel must not report last week's connection."""

    def test_register_installs_a_load_handler(self):
        with fake_bpy.installed() as bpy:
            props = importlib.import_module("showcade_bridge.props")
            props.register()
            self.assertEqual(
                len(bpy.app.handlers.load_post),
                1,
                "nothing clears the status on load — a saved .blend reopens showing the "
                "previous session's connection line as if it were live",
            )

    def test_the_handler_blanks_a_restored_status(self):
        with fake_bpy.installed() as bpy:
            props = importlib.import_module("showcade_bridge.props")
            props.register()
            # What a reopened .blend looks like: the property came back from the file.
            stale = fake_bpy.FakeScene("connected to http://stale:8899 — token accepted")
            fresh = fake_bpy.FakeScene("")
            bpy.data.scenes = [stale, fresh]
            for handler in bpy.app.handlers.load_post:
                handler(None, None)
            self.assertEqual(stale.showcade.status, "")
            self.assertEqual(fresh.showcade.status, "")

    def test_the_handler_survives_the_load_it_reacts_to(self):
        # A non-@persistent handler is dropped by the file load itself, so it
        # would clear the status exactly once and then never again.
        with fake_bpy.installed():
            props = importlib.import_module("showcade_bridge.props")
            self.assertTrue(
                getattr(props._clear_status_on_load, "_fake_bpy_persistent", False),
                "the load handler must be @bpy.app.handlers.persistent",
            )

    def test_unregister_removes_the_handler(self):
        # A disable/enable cycle must not stack duplicate handlers.
        with fake_bpy.installed() as bpy:
            props = importlib.import_module("showcade_bridge.props")
            props.register()
            props.register()
            self.assertEqual(len(bpy.app.handlers.load_post), 1)
            props.unregister()
            self.assertEqual(bpy.app.handlers.load_post, [])

    def test_clear_status_only_touches_what_it_has_to(self):
        scenes = [fake_bpy.FakeScene("stale"), fake_bpy.FakeScene(""), object()]
        with fake_bpy.installed():
            props = importlib.import_module("showcade_bridge.props")
            self.assertEqual(props.clear_status(scenes), 1)
            self.assertEqual(scenes[0].showcade.status, "")


class SelectionIsMeasuredAsAWhole(unittest.TestCase):
    """bd showcade-x9r7 — the union of the selection, not the biggest object."""

    def measure(self, objects, scale_length=1.0):
        with fake_bpy.installed():
            ops = importlib.import_module("showcade_bridge.ops")
            return ops.max_dimension_m(objects, scale_length)

    def test_a_level_sized_spread_of_small_objects_warns(self):
        # THE case: nothing is bigger than 10 cm, but they span 30 m. The old
        # per-object measurement reported 0.1 m and the warning never fired.
        objects = [fake_bpy.FakeObject(size=0.1, location=(x, 0.0, 0.0)) for x in (0.0, 10.0, 20.0, 30.0)]
        metres = self.measure(objects)
        self.assertGreater(metres, policy.MAX_REASONABLE_M)
        self.assertIsNotNone(policy.size_warning(metres))

    def test_a_prop_sized_selection_stays_silent(self):
        objects = [
            fake_bpy.FakeObject(size=0.1, location=(0.0, 0.0, 0.0)),
            fake_bpy.FakeObject(size=0.1, location=(0.2, 0.0, 0.1)),
        ]
        metres = self.measure(objects)
        self.assertAlmostEqual(metres, 0.3)
        self.assertIsNone(policy.size_warning(metres))

    def test_a_single_object_is_still_its_own_size(self):
        metres = self.measure([fake_bpy.FakeObject(size=0.4)])
        self.assertAlmostEqual(metres, 0.4)

    def test_the_world_transform_is_applied_not_the_local_box(self):
        # A 10 cm cube scaled 100x in the world is 10 m, and the warning must see
        # the 10 m — `bound_box` alone would report 0.1.
        metres = self.measure([fake_bpy.FakeObject(size=0.1, scale=100.0)])
        self.assertAlmostEqual(metres, 10.0)
        self.assertIsNotNone(policy.size_warning(metres))

    def test_the_scene_unit_scale_is_applied(self):
        objects = [fake_bpy.FakeObject(size=0.1, location=(0.0, 0.0, 0.0))]
        self.assertAlmostEqual(self.measure(objects, scale_length=100.0), 10.0)

    def test_nothing_to_measure_is_zero_not_a_warning(self):
        self.assertEqual(self.measure([]), 0.0)
        self.assertEqual(self.measure([object()]), 0.0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


class NonGeometryObjectsAreNotMeasured(unittest.TestCase):
    """The union AABB must cover what SHIPS, not what happens to be selected.

    Verify-pass regression (showcade-x9r7 review): lights and cameras have a
    zero-size bound_box but a real world position, and EXPORT_KWARGS drops both
    (export_lights/export_cameras are False). Including them pulled the box out
    to wherever they sat — on Blender's DEFAULT startup scene (2 m Cube, camera
    at (7.4,-6.9,5.0), light at (4.1,5.9,5.9)) that read 12.8 m and fired the
    "scene-sized, not prop-sized" warning on the most ordinary send there is.
    """

    def test_the_default_startup_scene_does_not_warn(self):
        with fake_bpy.installed():
            ops = importlib.import_module("showcade_bridge.ops")
            objs = [
                fake_bpy.FakeObject(size=2.0, name="Cube", type="MESH"),
                fake_bpy.FakeObject(size=0.0, location=(7.36, -6.93, 4.96), name="Camera", type="CAMERA"),
                fake_bpy.FakeObject(size=0.0, location=(4.08, 5.90, 5.90), name="Light", type="LIGHT"),
            ]
            metres = ops.max_dimension_m(objs, 1.0)
            self.assertAlmostEqual(metres, 2.0, places=3)
            self.assertIsNone(policy.size_warning(metres))

    def test_a_genuinely_spread_MESH_selection_still_warns(self):
        # The bead's original case must keep working — this is not a revert.
        with fake_bpy.installed():
            ops = importlib.import_module("showcade_bridge.ops")
            objs = [
                fake_bpy.FakeObject(size=0.1, location=(0.0, 0.0, 0.0), name="A", type="MESH"),
                fake_bpy.FakeObject(size=0.1, location=(30.0, 0.0, 0.0), name="B", type="MESH"),
            ]
            metres = ops.max_dimension_m(objs, 1.0)
            self.assertGreater(metres, 5.0)
            self.assertIsNotNone(policy.size_warning(metres))


class MeasurementIsMemoisedForTheRedrawPath(unittest.TestCase):
    """max_dimension_m runs from panel.draw(), i.e. on every N-panel redraw."""

    def test_an_unchanged_selection_does_not_re_transform(self):
        with fake_bpy.installed():
            ops = importlib.import_module("showcade_bridge.ops")
            ops._bounds_memo["key"] = None
            calls = []
            objs = [fake_bpy.FakeObject(size=1.0, name="A", type="MESH")]
            original = ops.world_corners

            def counting(obj):
                calls.append(obj)
                return original(obj)

            ops.world_corners = counting
            try:
                first = ops.max_dimension_m(objs, 1.0)
                after_first = len(calls)
                second = ops.max_dimension_m(objs, 1.0)
                self.assertEqual(second, first)
                self.assertEqual(len(calls), after_first)  # no re-transform
            finally:
                ops.world_corners = original

    def test_moving_an_object_invalidates_the_memo(self):
        with fake_bpy.installed():
            ops = importlib.import_module("showcade_bridge.ops")
            ops._bounds_memo["key"] = None
            objs = [fake_bpy.FakeObject(size=0.1, name="A", type="MESH")]
            near = ops.max_dimension_m(objs, 1.0)
            objs.append(fake_bpy.FakeObject(size=0.1, location=(40.0, 0.0, 0.0), name="B", type="MESH"))
            far = ops.max_dimension_m(objs, 1.0)
            self.assertGreater(far, near)
