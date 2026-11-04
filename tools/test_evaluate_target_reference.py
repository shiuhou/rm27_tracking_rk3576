#!/usr/bin/env python3
"""Regression checks for sparse reference evaluation, using synthetic data only."""

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SPEC = importlib.util.spec_from_file_location(
    "evaluate_target_reference", Path(__file__).with_name("evaluate_target_reference.py")
)
EVALUATOR = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(EVALUATOR)


def reference(frame=0, green_status="visible", armor_status="visible"):
    return {
        "frame": frame,
        "split": "development",
        "green_status": green_status,
        "green_center": [50, 80] if green_status == "visible" else None,
        "green_radius": 4 if green_status == "visible" else None,
        "armor_status": armor_status,
        "armor_center": [50, 30] if armor_status == "visible" else None,
        "armor_bbox": [20, 20, 80, 40] if armor_status == "visible" else None,
    }


def detection(green_x=50, predicted=False, armor_scan=True, bar_x=(22, 78)):
    return {
        "green": {
            "valid": True,
            "predicted": predicted,
            "center_x": green_x,
            "center_y": 80,
        },
        "armor": {
            "valid": True,
            "center": {"x": 50, "y": 30},
            "left_bar": {"center": {"x": bar_x[0], "y": 30}},
            "right_bar": {"center": {"x": bar_x[1], "y": 30}},
        },
        "armor_detection_ran": armor_scan,
    }


class ReferenceEvaluationTests(unittest.TestCase):
    def evaluate(self, rows, refs):
        return EVALUATOR.evaluate(rows, refs)["development"]

    def test_missing_replay_frame_rejects_incomplete_comparison(self):
        with self.assertRaisesRegex(ValueError, "missing 1 reference frames"):
            self.evaluate({0: detection()}, [reference(0), reference(360)])

    def test_green_hits_require_correct_location_and_direct_observation(self):
        rows = {
            0: detection(green_x=53),
            1: detection(green_x=65),
            2: detection(predicted=True),
        }
        stats = self.evaluate(rows, [reference(i) for i in rows])["green"]
        self.assertEqual(stats["visible_reference_frames"], 3)
        self.assertEqual(stats["matched_direct_detections"], 1)
        self.assertEqual(stats["wrong_location_detections"], 1)
        self.assertEqual(stats["missed_or_wrong"], 2)
        self.assertEqual(stats["matched_center_error_median_px"], 3)
        self.assertEqual(stats["matched_center_error_max_px"], 3)

    def test_cached_armor_is_not_a_direct_detection(self):
        stats = self.evaluate(
            {0: detection(), 1: detection(armor_scan=False)},
            [reference(0), reference(1)],
        )["armor"]
        self.assertEqual(stats["visible_reference_frames"], 2)
        self.assertEqual(stats["matched_direct_detections"], 1)
        self.assertEqual(stats["missed_or_wrong"], 1)
        self.assertEqual(stats["wrong_location_detections"], 0)

    def test_near_center_fragments_do_not_match_two_widely_separated_bars(self):
        stats = self.evaluate(
            {0: detection(), 1: detection(bar_x=(49, 51))},
            [reference(0), reference(1)],
        )["armor"]
        self.assertEqual(stats["matched_direct_detections"], 1)
        self.assertEqual(stats["wrong_location_detections"], 1)
        self.assertEqual(stats["missed_or_wrong"], 1)

    def test_uncertain_references_do_not_enter_positive_or_negative_counts(self):
        refs = [reference(0), reference(1, "absent", "absent"),
                reference(2, "uncertain", "uncertain")]
        stats = self.evaluate({i: detection() for i in range(3)}, refs)
        for kind in ("green", "armor"):
            with self.subTest(kind=kind):
                self.assertEqual(stats[kind]["visible_reference_frames"], 1)
                self.assertEqual(stats[kind]["absent_reference_frames"], 1)
                self.assertEqual(stats[kind]["matched_direct_detections"], 1)
                self.assertEqual(stats[kind]["false_detections_on_absent"], 1)
                self.assertEqual(stats[kind]["uncertain_excluded"], 1)

    def test_predicted_or_cached_outputs_are_not_direct_false_detections(self):
        rows = {0: detection(predicted=True, armor_scan=False)}
        stats = self.evaluate(rows, [reference(0, "absent", "absent")])
        for kind in ("green", "armor"):
            with self.subTest(kind=kind):
                self.assertEqual(stats[kind]["absent_reference_frames"], 1)
                self.assertEqual(stats[kind]["false_detections_on_absent"], 0)


def green_reference(frame=0, status="visible", **updates):
    ref = {"frame": frame, "split": "regression", "green_status": status,
           "green_center": [50, 80] if status == "visible" else None,
           "green_radius": 4 if status == "visible" else None,
           "review_state": "ai_provisional", "sample_group": "uniform",
           "annotation_source": "synthetic_fixture"}
    ref.update(updates)
    return ref


def green_row(frame=0, x=50, *, fresh=True, candidates=1, predicted=False):
    return {"frame": frame, "timestamp_us": round(frame * 1e6 / 180),
            "replay_timestamps": "nominal_source_fps", "predicted": predicted,
            "candidate_count": candidates, "candidates": [{}] * candidates,
            "green": {"valid": fresh or predicted, "predicted": predicted,
                      "center_x": x + 1, "center_y": 80, "state": "TRACKING" if fresh else "LOST"},
            "pixel_measurement": {"valid": fresh, "new_measurement": fresh,
                                  "tracker_confirmed": fresh,
                                  "selected_count": 1 if fresh else 0,
                                  "center": {"valid": fresh, "x": x, "y": 80},
                                  "source_width": 1344 if fresh else 0,
                                  "source_height": 760 if fresh else 0}}


class GreenQualityTests(unittest.TestCase):
    def evaluate(self, rows, refs, source="pixel_measurement"):
        return EVALUATOR.evaluate(rows, refs, kind="green", observation_source=source,
                                  width=1344, height=760)["regression"]

    def test_four_visible_stages_and_wrong_errors_are_retained(self):
        rows = {0: green_row(0, fresh=False, candidates=0),
                1: green_row(1, fresh=False), 2: green_row(2, x=90),
                3: green_row(3, x=53)}
        stats = self.evaluate(rows, [green_reference(i) for i in rows])["groups"][0]["green"]
        self.assertEqual(stats["stage_counts"], dict.fromkeys(EVALUATOR.STAGES, 1))
        self.assertEqual(stats["matched_direct_detections"], 1)
        self.assertEqual(stats["wrong_location_detections"], 1)
        self.assertEqual(stats["missed_or_wrong"], 3)
        self.assertEqual(stats["all_observed_center_error_max_px"], 40)
        self.assertEqual(stats["all_observed_center_error_median_px"], 21.5)
        self.assertEqual(stats["sample_results"][2]["center_error_px"], 40)

    def test_direct_pixel_is_not_posterior_and_prediction_is_not_observation(self):
        rows = {0: green_row(0), 1: green_row(1, fresh=False, predicted=True),
                2: green_row(2, fresh=True, predicted=True)}
        refs = [green_reference(i) for i in rows]
        stats = self.evaluate(rows, refs)["groups"][0]["green"]
        self.assertEqual(stats["matched_direct_detections"], 1)
        first, predicted, contradictory = stats["sample_results"]
        self.assertEqual(first["center_error_px"], 0)
        self.assertEqual(first["posterior_center_error_px"], 1)
        self.assertIsNone(predicted["center_error_px"])
        self.assertEqual(predicted["posterior_center_error_px"], 1)
        self.assertIsNone(contradictory["center_error_px"])
        posterior = self.evaluate({0: rows[0]}, [refs[0]], source="green")["groups"][0]["green"]
        self.assertEqual(posterior["sample_results"][0]["center_error_px"], 1)

    def test_each_confirmation_flag_and_single_selection_is_required(self):
        for key, value in (("valid", False), ("new_measurement", False),
                           ("tracker_confirmed", False), ("selected_count", 0),
                           ("selected_count", 2)):
            with self.subTest(key=key, value=value):
                row = green_row()
                row["pixel_measurement"][key] = value
                stats = self.evaluate({0: row}, [green_reference()])["groups"][0]["green"]
                self.assertEqual(stats["matched_direct_detections"], 0)
        row = green_row()
        row["pixel_measurement"]["center"]["valid"] = False
        stats = self.evaluate({0: row}, [green_reference()])["groups"][0]["green"]
        self.assertEqual(stats["matched_direct_detections"], 0)

    def test_review_state_and_sample_group_never_mix(self):
        refs = [green_reference(0), green_reference(1, review_state="human_reviewed"),
                green_reference(2, sample_group="diagnostic_case"),
                green_reference(3, review_state="pending")]
        groups = self.evaluate({i: green_row(i) for i in range(4)}, refs)["groups"]
        self.assertEqual(len(groups), 4)
        by_key = {(group["review_state"], group["sample_group"]): group["green"] for group in groups}
        self.assertEqual(by_key[("human_reviewed", "uniform")]["visible_reference_frames"], 1)
        self.assertEqual(by_key[("ai_provisional", "diagnostic_case")]["visible_reference_frames"], 1)
        pending = by_key[("pending", "uniform")]
        self.assertEqual(pending["pending_excluded"], 1)
        self.assertEqual(pending["visible_reference_frames"], 0)

    def test_uncertain_and_pending_excluded_absent_counts_only_direct(self):
        refs = [green_reference(0, "uncertain"), green_reference(1, "pending"),
                green_reference(2, "absent"), green_reference(3, "absent")]
        rows = {i: green_row(i) for i in range(3)}
        rows[3] = green_row(3, fresh=False, predicted=True)
        stats = self.evaluate(rows, refs)["groups"][0]["green"]
        self.assertEqual(stats["visible_reference_frames"], 0)
        self.assertEqual(stats["uncertain_excluded"], 1)
        self.assertEqual(stats["pending_excluded"], 1)
        self.assertEqual(stats["absent_reference_frames"], 2)
        self.assertEqual(stats["false_detections_on_absent"], 1)
        self.assertTrue(all(count == 0 for count in stats["stage_counts"].values()))

    def test_duplicate_missing_and_invalid_references_rejected(self):
        rows = {0: green_row()}
        with self.assertRaisesRegex(ValueError, "Duplicate reference"):
            self.evaluate(rows, [green_reference(), green_reference()])
        with self.assertRaisesRegex(ValueError, "missing 1 reference"):
            self.evaluate(rows, [green_reference(), green_reference(1)])
        for update in ({"green_center": [float("nan"), 80]}, {"green_center": [1344, 80]},
                       {"green_radius": 0}, {"review_state": "approved_by_ai"},
                       {"sample_group": "selected_success"}):
            with self.subTest(update=update), self.assertRaises(ValueError):
                self.evaluate(rows, [green_reference(**update)])

    def test_valid_pixel_and_posterior_coordinates_must_be_finite_in_source_bounds(self):
        for location, value in (("pixel", float("inf")), ("pixel", -1),
                                ("pixel", 1344), ("green", 1344)):
            with self.subTest(location=location, value=value), self.assertRaises(ValueError):
                row = green_row()
                if location == "pixel":
                    row["pixel_measurement"]["center"]["x"] = value
                else:
                    row["green"]["center_x"] = value
                self.evaluate({0: row}, [green_reference()])
        row = green_row()
        row["pixel_measurement"]["source_width"] = 640
        with self.assertRaisesRegex(ValueError, "source dimensions"):
            self.evaluate({0: row}, [green_reference()])

    def test_supplied_fresh_pixel_timestamp_and_age_must_be_current(self):
        for update in ({"timestamp_us": 1}, {"measurement_age_us": 1},
                       {"measurement_age_us": False}):
            with self.subTest(update=update), self.assertRaises(ValueError):
                row = green_row()
                row["pixel_measurement"].update(update)
                self.evaluate({0: row}, [green_reference()])

    def test_candidate_metadata_and_top_level_prediction_types_are_checked(self):
        for update in ({"candidate_count": 2}, {"predicted": "false"},
                       {"candidate_count": True}):
            with self.subTest(update=update), self.assertRaises(ValueError):
                row = green_row()
                row.update(update)
                self.evaluate({0: row}, [green_reference()])


class ReplayStreamTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "replay.jsonl"

    def write_rows(self, rows):
        with self.path.open("w") as stream:
            for row in rows:
                stream.write(json.dumps(row) + "\n")

    def read(self, frames=(0,), count=3):
        return EVALUATOR.read_replay(self.path, frames, stream_audit=True,
                                     expected_frames=count, fps=180, width=1344, height=760)

    def test_full_stream_audit_retains_only_sparse_rows(self):
        self.write_rows(green_row(i, fresh=i % 4 == 0, candidates=int(i % 4 == 0),
                                  predicted=i % 4 == 1) for i in range(2500))
        rows, audit = self.read((0, 1080, 2160), count=2500)
        self.assertEqual(set(rows), {0, 1080, 2160})
        self.assertEqual(audit["row_count"], 2500)
        self.assertEqual(audit["retained_reference_rows"], 3)
        self.assertEqual(audit["fresh_confirmed_pixel_frames"], 625)
        self.assertEqual(audit["green_predicted_valid_frames"], 625)
        self.assertEqual(audit["longest_without_fresh_observation_frames"], 3)
        self.assertEqual(audit["intervals_without_fresh_observation"], 625)
        self.assertIn("not exposure", audit["timing_provenance"])

    def test_duplicate_reordered_and_truncated_streams_rejected(self):
        for indices in ((0, 1, 1), (0, 2, 1), (0, 1), (1, 2, 3), (0, 2, 3)):
            with self.subTest(indices=indices), self.assertRaises(ValueError):
                self.write_rows(green_row(i) for i in indices)
                self.read()

    def test_missing_reference_and_hash_mismatch_rejected(self):
        self.write_rows(green_row(i) for i in range(3))
        with self.assertRaisesRegex(ValueError, "missing 1 reference"):
            self.read((0, 3))
        with self.assertRaisesRegex(ValueError, "SHA-256 mismatch"):
            EVALUATOR.read_replay(self.path, (0,), expected_sha256="0" * 64)

    def test_gaps_are_counted_without_expected_complete_sequence(self):
        self.write_rows(green_row(i) for i in (0, 1, 4))
        _, audit = self.read(count=None)
        self.assertEqual(audit["missing_sequence_frames"], 2)
        self.assertEqual(audit["gap_events"], 1)

    def test_invalid_json_blank_records_and_nominal_timestamps_rejected(self):
        for content in ('{"frame":', '\n', '{"frame": 0}\nnull\n'):
            with self.subTest(content=content), self.assertRaises(ValueError):
                self.path.write_text(content)
                self.read()
        for timestamp, provenance in ((10, "nominal_source_fps"), (0, "exposure")):
            with self.subTest(timestamp=timestamp, provenance=provenance), self.assertRaises(ValueError):
                rows = [green_row(i) for i in range(3)]
                rows[1]["timestamp_us"] = timestamp
                rows[1]["replay_timestamps"] = provenance
                self.write_rows(rows)
                self.read()

    def test_all_rows_validated_not_just_labelled_rows(self):
        rows = [green_row(i) for i in range(3)]
        rows[1]["pixel_measurement"]["center"]["x"] = float("nan")
        self.write_rows(rows)
        with self.assertRaisesRegex(ValueError, "finite"):
            self.read((0, 2))

    def test_cli_new_mode_is_exclusive_and_tentative_never_formally_passes(self):
        self.write_rows(green_row(i) for i in range(3))
        ref_path, out_path = self.root / "reference.jsonl", self.root / "result.json"
        ref_path.write_text(json.dumps(green_reference()) + "\n")
        command = [sys.executable, str(Path(EVALUATOR.__file__)),
                   "--reference", str(ref_path), "--replay", str(self.path),
                   "--output", str(out_path), "--kind", "green",
                   "--observation-source", "pixel_measurement", "--width", "1344", "--height", "760",
                   "--stream-audit", "--expected-frames", "3", "--fps", "180"]
        first = subprocess.run(command, text=True, capture_output=True)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(json.loads(out_path.read_text())["formal_quality_status"], "BLOCKED")
        original = out_path.read_bytes()
        second = subprocess.run(command, text=True, capture_output=True)
        self.assertNotEqual(second.returncode, 0)
        self.assertEqual(out_path.read_bytes(), original)

    def test_legacy_cli_preserves_both_kinds_and_output_overwrite(self):
        row = detection()
        row["frame"] = 0
        self.write_rows([row])
        ref_path, out_path = self.root / "reference.jsonl", self.root / "result.json"
        ref_path.write_text(json.dumps(reference()) + "\n")
        out_path.write_text("previous output")
        command = [sys.executable, str(Path(EVALUATOR.__file__)),
                   "--reference", str(ref_path), "--replay", str(self.path),
                   "--output", str(out_path)]
        result = subprocess.run(command, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        stats = json.loads(out_path.read_text())["results"]["development"]
        self.assertEqual(stats["green"]["matched_direct_detections"], 1)
        self.assertEqual(stats["armor"]["matched_direct_detections"], 1)


if __name__ == "__main__":
    unittest.main()
