# SPDX-FileCopyrightText: 2026 caderuntime <caderuntime@cade.run>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Pure-unit tests for the operator's decisions (`policy.py`).

No Blender, no server: these are the rules the Send button obeys, expressed as
counts in and a plan out. The one that matters most is negative — **an empty
selection must never resolve to the whole scene**, which is how a 200 MB scene
gets uploaded by accident.
"""

from __future__ import annotations

import unittest
from urllib.parse import parse_qs, urlparse

import fake_assets  # noqa: F401  (puts the repo root on sys.path)

from showcade_bridge import policy


class SourceResolution(unittest.TestCase):
    def resolve(self, source="SELECTION", *, selected=0, collection="Props", in_collection=0, in_scene=0):
        return policy.resolve_source(
            source,
            selected_count=selected,
            collection_name=collection,
            collection_count=in_collection,
            scene_count=in_scene,
        )

    def test_selection_with_objects_exports_the_selection(self):
        plan = self.resolve(selected=3, in_collection=9, in_scene=40)
        self.assertEqual(plan.scope, "SELECTION")
        self.assertTrue(plan.kwargs["use_selection"])
        self.assertFalse(plan.fell_back)
        self.assertIn("3 selected objects", plan.label)

    def test_an_empty_selection_never_becomes_the_whole_scene(self):
        # THE load-bearing rule. A populated scene plus an empty selection must
        # resolve to the active collection — never to every object there is.
        plan = self.resolve(selected=0, collection="Props", in_collection=2, in_scene=400)
        self.assertEqual(plan.scope, "COLLECTION")
        self.assertTrue(plan.fell_back)
        self.assertFalse(plan.kwargs["use_selection"])
        self.assertTrue(plan.kwargs["use_active_collection"])
        self.assertTrue(plan.kwargs["use_active_collection_with_nested"])

    def test_an_empty_selection_and_an_empty_collection_is_an_error(self):
        with self.assertRaises(policy.SourceError) as caught:
            self.resolve(selected=0, collection="Props", in_collection=0, in_scene=400)
        # The message must name the fix, not the condition.
        self.assertIn("select", str(caught.exception).lower())

    def test_the_fallback_never_intersects_with_an_empty_selection(self):
        # `use_selection` staying True alongside `use_active_collection` would
        # export the intersection — i.e. nothing at all — with no error anywhere.
        for source in ("SELECTION", "COLLECTION"):
            plan = self.resolve(source, selected=0, in_collection=5, in_scene=5)
            self.assertFalse(plan.kwargs["use_selection"], source)

    def test_the_whole_scene_is_only_ever_an_explicit_choice(self):
        plan = self.resolve("SCENE", selected=0, in_collection=0, in_scene=7)
        self.assertEqual(plan.scope, "SCENE")
        self.assertFalse(plan.kwargs["use_selection"])
        self.assertFalse(plan.kwargs.get("use_active_collection", False))
        self.assertFalse(plan.fell_back)

    def test_an_empty_scene_cannot_be_sent(self):
        with self.assertRaises(policy.SourceError):
            self.resolve("SCENE", in_scene=0)

    def test_collection_source_with_no_collection_is_an_error(self):
        with self.assertRaises(policy.SourceError):
            self.resolve("COLLECTION", collection=None, in_collection=0)

    def test_an_unknown_source_is_rejected(self):
        with self.assertRaises(policy.SourceError):
            self.resolve("EVERYTHING", selected=1)

    def test_every_dropdown_item_resolves(self):
        # The enum items and the resolver must not drift apart: an item the
        # resolver rejects would be an unreachable dropdown entry.
        for ident, _label, _desc in policy.SOURCE_ITEMS:
            plan = self.resolve(ident, selected=2, in_collection=2, in_scene=2)
            self.assertIn(plan.scope, {"SELECTION", "COLLECTION", "SCENE"}, ident)

    def test_resolution_is_deterministic(self):
        first = self.resolve(selected=2, in_collection=3, in_scene=4)
        second = self.resolve(selected=2, in_collection=3, in_scene=4)
        self.assertEqual(first, second)


def box_corners(size: float, at=(0.0, 0.0, 0.0)):
    """The 8 world-space corners of an axis-aligned cube centred on `at`."""
    half = size / 2.0
    return [
        (at[0] + x * half, at[1] + y * half, at[2] + z * half)
        for x in (-1.0, 1.0)
        for y in (-1.0, 1.0)
        for z in (-1.0, 1.0)
    ]


class SelectionBounds(unittest.TestCase):
    """What the >5 m warning actually measures (bd showcade-x9r7).

    The whole selection's box. Measuring each object's OWN box made the warning
    blind to its motivating case: "I selected the entire level" is many small
    objects spread over 30 m, where no single object is anywhere near 5 m.
    """

    def test_the_union_is_measured_not_the_largest_object(self):
        points = box_corners(0.1) + box_corners(0.1, (30.0, 0.0, 0.0))
        metres = policy.bounds_max_dimension_m(points, 1.0)
        self.assertAlmostEqual(metres, 30.1)
        self.assertIsNotNone(policy.size_warning(metres))

    def test_a_prop_sized_group_stays_prop_sized(self):
        points = box_corners(0.1) + box_corners(0.1, (0.2, 0.0, 0.0))
        self.assertAlmostEqual(policy.bounds_max_dimension_m(points, 1.0), 0.3)

    def test_the_longest_axis_wins(self):
        points = [(0.0, 0.0, 0.0), (1.0, 2.0, 9.0)]
        self.assertAlmostEqual(policy.bounds_max_dimension_m(points, 1.0), 9.0)

    def test_a_span_across_the_origin_is_not_cancelled_out(self):
        # Signed coordinates: -15 → +15 is 30 m across, not 15 and not 0.
        points = [(-15.0, 0.0, 0.0), (15.0, 0.0, 0.0)]
        self.assertAlmostEqual(policy.bounds_max_dimension_m(points, 1.0), 30.0)

    def test_the_unit_scale_converts_to_metres(self):
        points = box_corners(0.1)
        self.assertAlmostEqual(policy.bounds_max_dimension_m(points, 100.0), 10.0)

    def test_nothing_to_measure_is_zero(self):
        self.assertEqual(policy.bounds_max_dimension_m([], 1.0), 0.0)

    def test_a_single_point_has_no_extent(self):
        self.assertEqual(policy.bounds_max_dimension_m([(3.0, 3.0, 3.0)], 1.0), 0.0)


class SizeWarning(unittest.TestCase):
    def test_prop_sized_boxes_are_silent(self):
        for metres in (0.01, 0.1, 0.5, 1.6, 5.0):
            self.assertIsNone(policy.size_warning(metres), metres)

    def test_scene_sized_and_speck_sized_both_warn(self):
        self.assertIsNotNone(policy.size_warning(policy.MAX_REASONABLE_M + 0.001))
        self.assertIsNotNone(policy.size_warning(policy.MIN_REASONABLE_M / 2))

    def test_an_unmeasurable_selection_is_not_a_warning(self):
        # Empties/lights have no dimensions; 0 means "nothing to measure", which
        # is not the same claim as "this model is 0 m across".
        self.assertIsNone(policy.size_warning(0.0))

    def test_the_warning_quotes_the_measurement(self):
        self.assertIn("12", policy.size_warning(12.0))


class GenerationWindow(unittest.TestCase):
    """The two-generation window the storage GC deletes from."""

    def test_a_new_asset_advances_the_window(self):
        last, prev = policy.rotate_generations(last="A", prev="", new="B")
        self.assertEqual((last, prev), ("B", "A"))

    def test_a_dedupe_does_not_advance_the_window(self):
        # The catalog returns the EXISTING row for unchanged bytes. Rotating on
        # it would put the live asset in `prev`, so the next send would delete
        # the generation the browser is still loading — one back, not two.
        self.assertEqual(policy.rotate_generations(last="A", prev="Z", new="A"), ("A", "Z"))

    def test_the_gc_target_is_always_two_generations_back(self):
        # Invariant, over an arbitrary run of sends with dedupes mixed in: the
        # asset `prev` points at is never the one `last` points at.
        last, prev = "", ""
        for new in ("A", "A", "B", "B", "B", "C", "D", "D"):
            last, prev = policy.rotate_generations(last=last, prev=prev, new=new)
            self.assertNotEqual(prev, last, f"after {new}")
        self.assertEqual((last, prev), ("D", "C"))

    def test_an_empty_result_is_ignored(self):
        self.assertEqual(policy.rotate_generations(last="A", prev="Z", new=""), ("A", "Z"))


class ClipboardHandle(unittest.TestCase):
    def test_the_handle_parses_back_to_its_fields(self):
        handle = policy.clipboard_handle(
            asset_id="01ABC", sha256="ff" * 32, name="Bumper & Co", link_id="0123456789abcdef"
        )
        parsed = urlparse(handle)
        self.assertEqual(f"{parsed.scheme}:{parsed.path}", policy.HANDLE_SCHEME)
        fields = parse_qs(parsed.query)
        # `&` in the name would truncate the handle if it were not encoded.
        self.assertEqual(fields["name"], ["Bumper & Co"])
        self.assertEqual(fields["id"], ["01ABC"])
        self.assertEqual(fields["link"], ["0123456789abcdef"])

    def test_empty_fields_are_omitted_not_blank(self):
        handle = policy.clipboard_handle(asset_id="01ABC")
        self.assertNotIn("sha256=", handle)
        self.assertNotIn("link=", handle)

    def test_a_handle_without_an_asset_is_refused(self):
        with self.assertRaises(ValueError):
            policy.clipboard_handle(asset_id="")

    def test_the_handle_is_stable_for_the_same_input(self):
        args = {"asset_id": "01ABC", "sha256": "ab" * 32, "name": "Ramp", "link_id": "0123456789abcdef"}
        self.assertEqual(policy.clipboard_handle(**args), policy.clipboard_handle(**args))


class ResultLine(unittest.TestCase):
    def test_a_dedupe_reads_as_success_not_as_a_failure(self):
        line = policy.summarize_upload("Bumper", status=200, sent_bytes=0, asset_id="01X")
        self.assertIn("unchanged", line)
        self.assertNotIn("fail", line.lower())

    def test_a_fresh_upload_reports_what_it_sent(self):
        line = policy.summarize_upload("Bumper", status=201, sent_bytes=2 * 1024 * 1024, asset_id="01X")
        self.assertIn("2.00 MB", line)

    def test_a_metadata_only_create_does_not_claim_to_have_uploaded_bytes(self):
        line = policy.summarize_upload("Bumper", status=201, sent_bytes=0, asset_id="01X")
        self.assertIn("metadata", line)
        self.assertNotIn("MB", line)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
