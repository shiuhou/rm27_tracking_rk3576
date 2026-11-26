"""Pure checks for regression comparisons and posthoc human-label handling."""
import copy
import importlib.util
from pathlib import Path
import tempfile
import unittest


MODULE = Path(__file__).resolve().parents[1] / "tools/video_replay/compare_recording.py"
SPEC = importlib.util.spec_from_file_location("compare_recording", MODULE)
compare = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(compare)


def output(frame=7, center=None):
    diag = {name: 0 for name in compare.COUNTERS}
    diag.update(schema_version=1, enabled=True, classical_ran=True, source_region=False,
                search_mode="full_frame", search_rect={"x": 0, "y": 0, "width": 100, "height": 100},
                sample_capacity=128, samples_dropped=0, peaks_unvisited=0,
                candidate_count=0, selected_index=-1, inside_cone_count=0, score_passed_count=0,
                tracker_state_after="LOST", tracker_confirmed_before=False, tracker_confirmed_after=False,
                selected_passes_association_gate=False, normalized_enabled=True, sparse_search=True,
                legacy_candidates_enabled=False, saturated_core_candidates_enabled=False,
                reason_counts={"accepted": 0, "appearance_color_fraction": 0}, samples=[])
    row = {"classical_detection_ran": True, "green": {"state": "LOST"},
           "rgb_stream": {"source_sequence": frame, "receipt_timestamp_us": 123, "width": 100, "height": 100},
           "pixel_measurement": {"valid": center is not None, "new_measurement": center is not None,
                                 "tracker_confirmed": center is not None, "source_metadata_valid": True,
                                 "selected_count": 1 if center else 0, "source_sequence": frame,
                                 "timestamp_us": 123, "measurement_age_us": 0,
                                 "center": {"valid": center is not None,
                                            "x": center[0] if center else 0, "y": center[1] if center else 0}},
           "stage_diagnostics": diag}
    return row


class ComparisonTests(unittest.TestCase):
    def test_only_documented_timing_and_additive_diagnostics_are_excluded(self):
        baseline = output()
        candidate = copy.deepcopy(baseline)
        baseline["classical_detection_ms"] = 1
        candidate["classical_detection_ms"] = 99
        candidate["stage_diagnostics"]["tile_seeds"] = 10
        candidate["rgb_stream"]["processing_ms"] = 5
        self.assertEqual(compare.comparable(baseline), compare.comparable(candidate))
        candidate["rgb_stream"]["receipt_timestamp_us"] += 1
        self.assertNotEqual(compare.comparable(baseline), compare.comparable(candidate))

    def test_diagnostics_require_current_output_and_conserved_samples(self):
        row = output()
        compare.validate_diagnostics(row)
        row["stage_diagnostics"]["tracker_state_after"] = "TRACKING"
        with self.assertRaisesRegex(ValueError, "tracker state"):
            compare.validate_diagnostics(row)
        row = output()
        row["stage_diagnostics"]["samples_dropped"] = 1
        with self.assertRaisesRegex(ValueError, "conservation"):
            compare.validate_diagnostics(row)

    def test_bounded_sample_drops_are_explicit_and_valid(self):
        row = output()
        diag = row["stage_diagnostics"]
        diag.update(samples=[{"x": 10, "y": 11, "diameter": 3, "response": 1,
                              "reason": "appearance_color_fraction"}],
                    samples_dropped=3, exact_evaluations=6, exact_passed=6, peaks_considered=4, peaks_unvisited=2)
        diag["reason_counts"]["appearance_color_fraction"] = 4
        compare.validate_diagnostics(row)
        diag["samples"] *= 129
        with self.assertRaisesRegex(ValueError, "capacity"):
            compare.validate_diagnostics(row)

    def test_exact_evaluation_counters_cannot_hide_lost_outcomes(self):
        row = output()
        row["stage_diagnostics"]["exact_evaluations"] = 1
        with self.assertRaisesRegex(ValueError, "exact-evaluation conservation"):
            compare.validate_diagnostics(row)

    def test_skipped_detection_cannot_keep_old_counters(self):
        row = output()
        row["classical_detection_ran"] = row["stage_diagnostics"]["classical_ran"] = False
        row["stage_diagnostics"]["tile_seeds"] = 1
        with self.assertRaisesRegex(ValueError, "stale"):
            compare.validate_diagnostics(row)

    def test_ai_fields_are_not_labels_and_pending_human_rows_fail(self):
        with tempfile.TemporaryDirectory() as directory:
            csv = Path(directory) / "review.csv"
            csv.write_text("frame_id,ai_status,ai_x,human_status,human_x,human_y,human_w,human_h\n"
                           "7,VISIBLE,999,NOT_PRESENT,,,,\n")
            self.assertEqual(compare.load_annotations(csv)[7]["human_status"], "NOT_PRESENT")
            csv.write_text("frame_id,human_status,human_x,human_y,human_w,human_h\n7,PENDING,,,,\n")
            with self.assertRaisesRegex(ValueError, "unreviewed"):
                compare.load_annotations(csv)

    def test_supplemental_cases_do_not_inflate_uniform_matches(self):
        annotations = {frame: {"human_status": "VISIBLE", "bbox_xywh": [10, 10, 10, 10]} for frame in (7, 9)}
        rows = {7: output(7), 9: output(9, [15, 15])}
        result = compare.score_annotations(annotations, rows, {9})["groups"]
        self.assertEqual(result["uniform"]["visible_frames"], 1)
        self.assertEqual(result["uniform"]["fresh_centers_inside_box"], 0)
        self.assertEqual(result["supplemental"]["fresh_centers_inside_box"], 1)

    def test_box_exclusive_edge_and_stale_pixels_are_not_matches(self):
        annotation = {7: {"human_status": "VISIBLE", "bbox_xywh": [10, 10, 10, 10]}}
        row = output(center=[20, 15])
        group = compare.score_annotations(annotation, {7: row}, set())["groups"]["uniform"]
        self.assertEqual(group["fresh_centers_outside_box"], 1)
        row["pixel_measurement"]["measurement_age_us"] = 1
        group = compare.score_annotations(annotation, {7: row}, set())["groups"]["uniform"]
        self.assertEqual(group["visible_without_fresh_center"], 1)

    def test_missing_samples_do_not_imply_a_local_rejection_cause(self):
        annotation = {7: {"human_status": "VISIBLE", "bbox_xywh": [10, 10, 10, 10]}}
        row = output()
        row["stage_diagnostics"]["samples_dropped"] = 100
        result = compare.score_annotations(annotation, {7: row}, set())["groups"]["uniform"]["frames"][0]
        self.assertEqual(result["diagnostics"]["sample_reasons_inside_box"], {})
        self.assertEqual(result["diagnostics"]["attribution"], "no_local_appearance_attribution_from_samples")
        self.assertEqual(result["diagnostics"]["samples_dropped_global"], 100)


if __name__ == "__main__":
    unittest.main()
