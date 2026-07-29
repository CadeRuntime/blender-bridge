# SPDX-FileCopyrightText: 2026 caderuntime <caderuntime@cade.run>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for the exporter kwargs + the RNA filter.

These are invariant assertions, not a golden copy of the dict: what matters is
that no compression path can ever be on, that nothing bakes a scale, and that the
filter only ever *removes* keys.
"""

from __future__ import annotations

import contextlib
import logging
import unittest
from typing import ClassVar

import fake_assets  # noqa: F401  (puts the repo root on sys.path)

import showcade_bridge
from showcade_bridge import exporter

#: The clamp warning is deliberately loud *in Blender* (that is bd showcade-py7c);
#: in a test run it is only noise on stderr. A NullHandler counts as "a handler
#: was found", which is what keeps `logging.lastResort` quiet — it does not stop
#: the capture below, which attaches its own.
_SILENCE = logging.NullHandler()


def setUpModule():
    logging.getLogger("showcade_bridge.exporter").addHandler(_SILENCE)


def tearDownModule():
    logging.getLogger("showcade_bridge.exporter").removeHandler(_SILENCE)


@contextlib.contextmanager
def captured_warnings():
    """The exporter's own log records, as a list of formatted messages.

    Hand-rolled rather than `assertLogs`/`assertNoLogs` so the "nothing was
    logged" half works on every Python the addon supports.
    """
    records: list[logging.LogRecord] = []

    class _Collect(logging.Handler):
        def emit(self, record):
            records.append(record)

    logger = logging.getLogger("showcade_bridge.exporter")
    handler = _Collect(level=logging.WARNING)
    logger.addHandler(handler)
    previous = logger.level
    logger.setLevel(logging.WARNING)
    try:
        yield records
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)


def messages(records) -> str:
    return "\n".join(record.getMessage() for record in records)

#: A stand-in for `bpy.ops.export_scene.gltf.get_rna_type().properties.keys()`.
ALL_NAMES = set(exporter.EXPORT_KWARGS) | {"filepath", "use_selection", "check_existing"}


class CompressionIsAlwaysOff(unittest.TestCase):
    def test_every_compression_switch_is_present_and_false(self):
        # Draco / native meshopt / gltfpack — modelLoader.ts registers none of
        # the decoder-backed extensions, so any of these silently yields a GLB
        # showcade cannot load.
        for switch in exporter.COMPRESSION_SWITCHES:
            with self.subTest(switch=switch):
                self.assertIn(switch, exporter.EXPORT_KWARGS)
                self.assertIs(exporter.EXPORT_KWARGS[switch], False)

    def test_all_three_paths_are_covered(self):
        self.assertEqual(
            exporter.COMPRESSION_SWITCHES,
            {
                "export_draco_mesh_compression_enable",
                "export_meshopt_compression_enable",
                "export_use_gltfpack",
            },
        )

    def test_no_kwarg_enables_compression_after_a_resolve(self):
        kwargs, _ = exporter.resolve_export_kwargs(ALL_NAMES, filepath="/tmp/x.glb")
        for switch in exporter.COMPRESSION_SWITCHES:
            self.assertIs(kwargs[switch], False)

    def test_an_override_cannot_re_enable_a_compression_switch(self):
        # The panel (showcade-q42r.5) passes caller overrides straight through,
        # so "the defaults are False" is not enough: the resolved kwargs must be
        # False for EVERY input, truthy or not (bd showcade-0yyi).
        for switch in sorted(exporter.COMPRESSION_SWITCHES):
            for value in (True, 1, "YES", object()):
                with self.subTest(switch=switch, value=value):
                    kwargs, _ = exporter.resolve_export_kwargs(
                        ALL_NAMES, filepath="/tmp/x.glb", **{switch: value}
                    )
                    self.assertIs(kwargs[switch], False)

    def test_every_switch_on_at_once_still_resolves_to_off(self):
        kwargs, _ = exporter.resolve_export_kwargs(
            ALL_NAMES,
            filepath="/tmp/x.glb",
            **dict.fromkeys(exporter.COMPRESSION_SWITCHES, True),
        )
        for switch in exporter.COMPRESSION_SWITCHES:
            self.assertIs(kwargs[switch], False)

    def test_clamping_is_narrow_and_leaves_other_overrides_alone(self):
        # The clamp must not degrade into "overrides are ignored" — only the
        # three compression names are forced.
        kwargs, _ = exporter.resolve_export_kwargs(
            ALL_NAMES,
            filepath="/tmp/x.glb",
            export_tangents=True,
            export_draco_mesh_compression_enable=True,
        )
        self.assertIs(kwargs["export_tangents"], True)
        self.assertIs(kwargs["export_draco_mesh_compression_enable"], False)

    def test_a_clamped_resolve_is_still_a_fixed_point(self):
        clamped, _ = exporter.resolve_export_kwargs(
            ALL_NAMES, filepath="/tmp/x.glb", export_use_gltfpack=True
        )
        plain, _ = exporter.resolve_export_kwargs(ALL_NAMES, filepath="/tmp/x.glb")
        # Attempting compression changes NOTHING about the resolved kwargs, and
        # re-resolving the clamped result reproduces it exactly.
        self.assertEqual(clamped, plain)
        again, dropped = exporter.resolve_export_kwargs(set(clamped), **clamped)
        self.assertEqual(again, clamped)
        self.assertEqual(dropped, [])

    def test_a_clamp_is_never_silent(self):
        # bd showcade-py7c: a refused override was invisible — absent from
        # `dropped` (which means "not in the RNA") and unlogged, against this
        # module's own rule that a setting which did not take effect must show.
        with captured_warnings() as records:
            kwargs, dropped = exporter.resolve_export_kwargs(
                ALL_NAMES, filepath="/tmp/x.glb", export_use_gltfpack=True
            )
        self.assertIs(kwargs["export_use_gltfpack"], False)
        self.assertNotIn("export_use_gltfpack", dropped, "a clamp is not a drop")
        self.assertIn("export_use_gltfpack", messages(records))

    def test_every_refused_switch_is_named(self):
        with captured_warnings() as records:
            exporter.resolve_export_kwargs(
                ALL_NAMES,
                filepath="/tmp/x.glb",
                **dict.fromkeys(exporter.COMPRESSION_SWITCHES, True),
            )
        text = messages(records)
        for switch in exporter.COMPRESSION_SWITCHES:
            self.assertIn(switch, text)

    def test_an_ordinary_resolve_is_quiet(self):
        # The warning has to mean something: no override, no noise.
        with captured_warnings() as records:
            exporter.resolve_export_kwargs(ALL_NAMES, filepath="/tmp/x.glb", export_tangents=True)
        self.assertEqual(messages(records), "")

    def test_explicitly_disabling_a_switch_is_not_a_refusal(self):
        with captured_warnings() as records:
            exporter.resolve_export_kwargs(
                ALL_NAMES, filepath="/tmp/x.glb", export_draco_mesh_compression_enable=False
            )
        self.assertEqual(messages(records), "")

    def test_the_refused_names_are_reportable_on_their_own(self):
        self.assertEqual(
            exporter.clamped_compression({"export_use_gltfpack": True, "export_tangents": True}),
            ["export_use_gltfpack"],
        )
        self.assertEqual(exporter.clamped_compression({"export_use_gltfpack": 0}), [])
        self.assertEqual(exporter.clamped_compression({}), [])
        self.assertEqual(
            exporter.clamped_compression(dict.fromkeys(exporter.COMPRESSION_SWITCHES, True)),
            sorted(exporter.COMPRESSION_SWITCHES),
        )

    def test_webp_is_off(self):
        # EXT_texture_webp IS registered, but the option emits a PNG fallback
        # too, inflating the GLB against the 32 MB cap for no gain.
        self.assertIs(exporter.EXPORT_KWARGS["export_image_add_webp"], False)
        self.assertIs(exporter.EXPORT_KWARGS["export_image_webp_fallback"], False)
        self.assertEqual(exporter.EXPORT_KWARGS["export_image_format"], "AUTO")


class NoBakedScale(unittest.TestCase):
    def test_nothing_touches_scale_or_units(self):
        # fitScale() owns metres→table-units; a pre-scaled GLB would lie to every
        # other consumer of the blob (ADR 0007 decision 2).
        for key in exporter.EXPORT_KWARGS:
            with self.subTest(key=key):
                self.assertNotIn("scale", key)
                self.assertNotIn("unit", key)

    def test_yup_is_on_so_nothing_pre_rotates(self):
        self.assertIs(exporter.EXPORT_KWARGS["export_yup"], True)

    def test_scene_furniture_and_animation_are_excluded(self):
        for key in ("export_animations", "export_lights", "export_cameras", "export_tangents"):
            self.assertIs(exporter.EXPORT_KWARGS[key], False)

    def test_modifiers_are_baked_into_a_single_glb(self):
        self.assertIs(exporter.EXPORT_KWARGS["export_apply"], True)
        self.assertEqual(exporter.EXPORT_KWARGS["export_format"], "GLB")


class RnaFilter(unittest.TestCase):
    def test_a_matching_rna_drops_nothing(self):
        kwargs, dropped = exporter.resolve_export_kwargs(ALL_NAMES, filepath="/tmp/x.glb")
        self.assertEqual(dropped, [])
        self.assertEqual(kwargs["filepath"], "/tmp/x.glb")

    def test_unknown_properties_are_dropped_and_reported(self):
        # Version drift: this Blender's exporter has no such property, and
        # passing it would be a hard TypeError.
        kwargs, dropped = exporter.resolve_export_kwargs(
            ALL_NAMES, filepath="/tmp/x.glb", export_colors=True
        )
        self.assertNotIn("export_colors", kwargs)
        self.assertEqual(dropped, ["export_colors"])

    def test_the_filter_never_invents_a_key(self):
        kwargs, _ = exporter.resolve_export_kwargs(
            ALL_NAMES | {"export_future_thing"}, filepath="/tmp/x.glb"
        )
        self.assertTrue(set(kwargs) <= set(exporter.EXPORT_KWARGS) | {"filepath"})

    def test_resolving_is_idempotent(self):
        first, dropped_first = exporter.resolve_export_kwargs(ALL_NAMES, filepath="/tmp/x.glb")
        second, dropped_second = exporter.resolve_export_kwargs(ALL_NAMES, filepath="/tmp/x.glb")
        self.assertEqual(first, second)
        self.assertEqual(dropped_first, dropped_second)
        # Re-resolving against exactly what survived is a fixed point.
        third, dropped_third = exporter.resolve_export_kwargs(set(first), **first)
        self.assertEqual(third, first)
        self.assertEqual(dropped_third, [])

    def test_overrides_win_over_the_defaults(self):
        kwargs, _ = exporter.resolve_export_kwargs(
            ALL_NAMES, filepath="/tmp/x.glb", use_selection=False
        )
        self.assertIs(kwargs["use_selection"], False)

    def test_a_missing_load_bearing_property_fails_loudly(self):
        # Dropping a compression switch silently would ship an unloadable GLB, so
        # its absence from the RNA must fail the export instead.
        for required in sorted(exporter.REQUIRED_KWARGS):
            with self.subTest(required=required), self.assertRaises(ValueError):
                exporter.resolve_export_kwargs(ALL_NAMES - {required}, filepath="/tmp/x.glb")

    def test_an_empty_rna_fails_rather_than_exporting_defaults(self):
        with self.assertRaises(ValueError):
            exporter.resolve_export_kwargs(set(), filepath="/tmp/x.glb")


class DeclaredBlenderFloor(unittest.TestCase):
    """`bl_info` must not advertise a Blender the export then refuses (bd showcade-e9u4).

    The compression-property set of each RELEASED glTF exporter, read from the
    released source (`glTF-Blender-IO`, `blender-v<x>-release`). The native
    meshopt switch — the one `REQUIRED_KWARGS` used to demand unconditionally —
    appears in **5.2**, and in no earlier release: requiring it hard-refused
    every 4.2–5.1 exporter with "this Blender's exporter is not supported", on
    versions the manifest advertises, over a compression path those exporters
    cannot perform at all.
    """

    #: version → the compression switches that exporter ships.
    EXPORTER_COMPRESSION: ClassVar[dict[tuple[int, int], set[str]]] = {
        (4, 2): {"export_draco_mesh_compression_enable", "export_use_gltfpack"},
        (4, 3): {"export_draco_mesh_compression_enable", "export_use_gltfpack"},
        (4, 5): {"export_draco_mesh_compression_enable", "export_use_gltfpack"},
        (5, 0): {"export_draco_mesh_compression_enable", "export_use_gltfpack"},
        (5, 1): {"export_draco_mesh_compression_enable", "export_use_gltfpack"},
        (5, 2): {
            "export_draco_mesh_compression_enable",
            "export_use_gltfpack",
            "export_meshopt_compression_enable",
        },
    }

    #: The non-compression names, all present since ≤4.2 (verified the same way).
    BASE_NAMES = (set(exporter.EXPORT_KWARGS) - exporter.COMPRESSION_SWITCHES) | {
        "filepath",
        "use_selection",
        "use_active_collection",
        "use_active_collection_with_nested",
    }

    @property
    def floor(self) -> tuple[int, int]:
        return tuple(showcade_bridge.bl_info["blender"])[:2]

    def test_every_advertised_blender_can_actually_export(self):
        for version, switches in sorted(self.EXPORTER_COMPRESSION.items()):
            if version < self.floor:
                continue
            with self.subTest(blender=f"{version[0]}.{version[1]}"):
                kwargs, dropped = exporter.resolve_export_kwargs(
                    self.BASE_NAMES | switches, filepath="/tmp/x.glb"
                )
                # Whatever compression this exporter CAN do is off…
                for switch in switches:
                    self.assertIs(kwargs[switch], False)
                # …and what it cannot do is absent, reported, and not fatal.
                for absent in exporter.COMPRESSION_SWITCHES - switches:
                    self.assertNotIn(absent, kwargs)
                    self.assertIn(absent, dropped)

    def test_the_floor_is_a_version_that_was_actually_checked(self):
        # An aspirational floor is exactly what this bead was about: the number
        # in bl_info has to name a release whose exporter someone enumerated.
        self.assertIn(self.floor, self.EXPORTER_COMPRESSION)

    def test_the_floors_exporter_has_no_unprovable_compression_path(self):
        # The reason a missing meshopt switch is safe on 4.2: that exporter has
        # no meshopt path at all, so there is nothing left enabled.
        self.assertNotIn("export_meshopt_compression_enable", self.EXPORTER_COMPRESSION[self.floor])
        self.assertTrue(self.EXPORTER_COMPRESSION[self.floor] >= exporter.UNIVERSAL_COMPRESSION_SWITCHES)

    def test_a_switch_the_exporter_does_have_is_still_load_bearing(self):
        # Relaxing the version-gated one must not relax the rest: draco and
        # gltfpack exist on every supported release, so their absence still
        # means "not the exporter we reasoned about" and must fail.
        for required in sorted(exporter.UNIVERSAL_COMPRESSION_SWITCHES | {"filepath", "export_format"}):
            with self.subTest(required=required):
                available = (self.BASE_NAMES | self.EXPORTER_COMPRESSION[(5, 2)]) - {required}
                with self.assertRaises(ValueError):
                    exporter.resolve_export_kwargs(available, filepath="/tmp/x.glb")


class ScaleLengthWarning(unittest.TestCase):
    def test_silent_at_unity_and_loud_otherwise(self):
        self.assertIsNone(exporter.scale_length_warning(1.0))
        self.assertIsNone(exporter.scale_length_warning(1.0 + 1e-12))
        for scale in (0.01, 0.5, 2.0, 1000.0):
            with self.subTest(scale=scale):
                message = exporter.scale_length_warning(scale)
                self.assertIsNotNone(message)
                self.assertIn(f"{scale:g}", message)

    def test_the_warning_never_claims_a_correction(self):
        message = exporter.scale_length_warning(0.01)
        self.assertIn("will not compensate", message)




class SafeStem(unittest.TestCase):
    """A user-supplied asset name must never escape the temp dir (showcade-q42r.4 review).

    The traversal was reproduced against a real Blender 5.2: exporting with
    ``name="../escaped"`` wrote a valid GLB to ``/tmp/escaped.glb``, which then
    SURVIVED ``TemporaryDirectory`` cleanup.
    """

    def test_relative_traversal_is_neutralised(self):
        self.assertEqual(exporter.safe_stem("../escaped"), "escaped")

    def test_absolute_path_is_neutralised(self):
        # os.path.join(tmpdir, "/abs/thing.glb") would DISCARD tmpdir entirely.
        stem = exporter.safe_stem("/home/john/thing")
        self.assertEqual(stem, "thing")

    def test_no_separator_survives(self):
        for raw in ("props/Bumper", "a\\b", "../../etc/passwd", "C:\\win\\x"):
            with self.subTest(raw=raw):
                stem = exporter.safe_stem(raw)
                self.assertNotIn("/", stem)
                self.assertNotIn("\\", stem)
                self.assertNotIn("..", stem)

    def test_join_never_leaves_the_temp_dir(self):
        # The property that actually matters, asserted the way export_glb uses it.
        import os.path as p
        tmpdir = "/tmp/showcade-glb-test"
        for raw in ("../escaped", "/abs/thing", "../../../root", "..", "/", "props/x"):
            with self.subTest(raw=raw):
                path = p.normpath(p.join(tmpdir, f"{exporter.safe_stem(raw)}.glb"))
                self.assertTrue(
                    path.startswith(tmpdir + "/"),
                    f"{raw!r} escaped to {path!r}",
                )

    def test_empty_and_all_junk_fall_back(self):
        for raw in ("", "..", "///", "!!!", "   "):
            with self.subTest(raw=raw):
                self.assertEqual(exporter.safe_stem(raw), "showcade")

    def test_ordinary_names_stay_readable(self):
        self.assertEqual(exporter.safe_stem("Slingshot cap"), "Slingshot_cap")
        self.assertEqual(exporter.safe_stem("bumper-001"), "bumper-001")

    def test_stem_is_bounded(self):
        self.assertLessEqual(len(exporter.safe_stem("x" * 500)), 64)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
