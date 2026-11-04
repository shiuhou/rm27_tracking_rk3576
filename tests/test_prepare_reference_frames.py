"""Native real-video extraction and fail-closed reference preparation."""
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import cv2
import numpy as np


SCRIPT = Path(__file__).resolve().parents[1] / "tools/dataset/prepare_reference_frames.py"
SPEC = importlib.util.spec_from_file_location("prepare_reference_frames", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class ReferencePreparationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.baseline = self.root / "baseline"
        self.baseline.mkdir()
        self.manifest = self.root / "manifest.json"
        self.output = self.root / "output"
        self.frame_count = 7
        video = self.root / "tiny.avi"
        writer = cv2.VideoWriter(str(video), cv2.VideoWriter_fourcc(*"FFV1"), 20, (32, 24))
        if not writer.isOpened():
            self.skipTest("OpenCV FFV1 encoder unavailable")
        self.frames = []
        for index in range(self.frame_count):
            frame = np.random.default_rng(index).integers(0, 256, (24, 32, 3), dtype=np.uint8)
            frame[3:9, 4:10] = (10, 120+index, 210)
            self.frames.append(frame)
            writer.write(frame)
        writer.release()
        rows = [dict(frame=index, timestamp_us=index * 50000,
                     green=dict(valid=False), pixel_measurement=dict(valid=False), candidates=[])
                for index in range(self.frame_count)]
        folder = self.baseline / "tiny"
        folder.mkdir()
        (folder / "detections.jsonl").write_text("".join(json.dumps(row)+"\n" for row in rows))
        semantics = hashlib.sha256()
        for row in rows:
            semantics.update(json.dumps(row, sort_keys=True, separators=(",", ":")).encode()+b"\n")
        (folder / "run.json").write_text(json.dumps(dict(
            status="PASS", input_sha256=MODULE.sha256(video),
            cli_summary=dict(frames=7, source_frames_read=7, source_width=32,
                             source_height=24, frame_step=1, native_resolution=True))))
        (self.baseline / "diagnostic.conf").write_text("example configuration\n")
        (self.root / "source.py").write_text("# a frozen source\n")
        catalog = dict(dataset_id="test-dataset", validation_environment=dict(opencv=cv2.__version__),
                       clips=[dict(id="tiny", path=video.name, sha256=MODULE.sha256(video),
                                   size_bytes=video.stat().st_size,
                                   encoded_video=dict(width=32, height=24, reported_fps=20,
                                                      reported_frame_count=7),
                                   validation=dict(sample_pixel_hashes=[dict(
                                       frame_index=index, pixel_sha256=hashlib.sha256(frame).hexdigest())
                                       for index, frame in enumerate(self.frames)]))])
        self.manifest.write_text(json.dumps(catalog))
        (self.baseline / "summary.json").write_text(json.dumps(dict(
            status="PASS", clips=[dict(clip_id="tiny", ordered_semantic_output_sha256=semantics.hexdigest())])))
        self.identity = self.baseline / "run_identity.json"
        self.identity.write_text(json.dumps(dict(dataset_id="test-dataset",
                                                manifest_sha256=MODULE.sha256(self.manifest),
                                                configuration_sha256=MODULE.sha256(self.baseline / "diagnostic.conf"),
                                                source_sha256={"source.py": MODULE.sha256(self.root / "source.py")})))

    def prepare(self, **kwargs):
        return MODULE.prepare(self.manifest, self.baseline, self.output,
                              stride=2, repo_root=self.root, **kwargs)

    def test_native_pixels_order_hashes_and_pending_references(self):
        result = self.prepare()
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["sample_count"], 4)
        self.assertEqual(result["clips"][0]["catalog_pixel_checks"], 7)
        samples = [json.loads(line) for line in (self.output / "samples.jsonl").read_text().splitlines()]
        self.assertEqual([row["frame"] for row in samples], [0, 2, 4, 6])
        for row in samples:
            image_path = self.output / row["image_path"]
            np.testing.assert_array_equal(cv2.imread(str(image_path)), self.frames[row["frame"]])
            self.assertEqual(MODULE.sha256(image_path), row["image_sha256"])
            self.assertEqual(hashlib.sha256(self.frames[row["frame"]]).hexdigest(),
                             row["native_bgr_pixel_sha256"])
        references = [json.loads(line) for line in
                      (self.output / "tiny/reference.jsonl").read_text().splitlines()]
        self.assertTrue(all(row["green_status"] == "pending" and row["green_center"] is None
                            and row["annotation_source"] == "unreviewed" for row in references))
        self.assertTrue(result["inputs_verified_before_and_after"])
        MODULE.render_review(self.output)
        self.assertTrue((self.output / "index.html").is_file())

    def test_existing_output_is_never_overwritten(self):
        self.output.mkdir()
        sentinel = self.output / "original"
        sentinel.write_text("retained")
        with self.assertRaises(FileExistsError):
            self.prepare()
        self.assertEqual(sentinel.read_text(), "retained")

    def test_wrong_source_hash_stops_before_decode(self):
        identity = json.loads(self.identity.read_text())
        identity["source_sha256"]["source.py"] = "0" * 64
        self.identity.write_text(json.dumps(identity))
        with mock.patch.object(MODULE.cv2, "VideoCapture") as capture:
            with self.assertRaisesRegex(ValueError, "SHA-256 mismatch"):
                self.prepare()
            capture.assert_not_called()
        self.assertEqual(json.loads((self.output / "summary.json").read_text())["status"], "FAIL")

    def test_semantic_corruption_fails_before_image_decode(self):
        path = self.baseline / "tiny/detections.jsonl"
        text = path.read_text()
        path.write_text(text.replace('"valid": false', '"valid": true', 1))
        with self.assertRaisesRegex(ValueError, "semantic hash"):
            self.prepare()
        self.assertFalse(any(self.output.rglob("*.png")))

    def test_output_limit_retains_bounded_explicit_failure(self):
        # Native images alone fit but the deliberately small complete artifact
        # budget does not: the final summary must still identify this failure.
        with self.assertRaisesRegex(OSError, "byte limit"):
            self.prepare(max_bytes=32768)
        summary = json.loads((self.output / "summary.json").read_text())
        self.assertEqual(summary["status"], "FAIL")
        self.assertLessEqual(sum(path.stat().st_size for path in self.output.rglob("*") if path.is_file()), 32768)

    def test_catalog_native_pixel_hash_mismatch_is_not_silently_accepted(self):
        catalog = json.loads(self.manifest.read_text())
        catalog["clips"][0]["validation"]["sample_pixel_hashes"][0]["pixel_sha256"] = "0" * 64
        self.manifest.write_text(json.dumps(catalog))
        identity = json.loads(self.identity.read_text())
        identity["manifest_sha256"] = MODULE.sha256(self.manifest)
        self.identity.write_text(json.dumps(identity))
        with self.assertRaisesRegex(ValueError, "native BGR pixel hash mismatch"):
            self.prepare()
        self.assertEqual(json.loads((self.output / "summary.json").read_text())["status"], "FAIL")

    def test_input_changes_during_decode_are_rejected(self):
        original = MODULE._open_video
        def changed_source(path):
            capture = original(path)
            (self.root / "source.py").write_text("# changed during decode\n")
            return capture
        with mock.patch.object(MODULE, "_open_video", changed_source):
            with self.assertRaisesRegex(ValueError, "input changed during extraction"):
                self.prepare()
        self.assertEqual(json.loads((self.output / "summary.json").read_text())["status"], "FAIL")

    def test_short_write_is_failure_not_success(self):
        original = MODULE._Budget.write
        def fail_png(budget, path, data, **kwargs):
            if path.suffix == ".png":
                raise OSError("simulated unavailable storage")
            return original(budget, path, data, **kwargs)
        with mock.patch.object(MODULE._Budget, "write", fail_png):
            with self.assertRaisesRegex(OSError, "unavailable storage"):
                self.prepare()
        self.assertEqual(json.loads((self.output / "summary.json").read_text())["status"], "FAIL")

    def test_decode_interruption_releases_resource_and_marks_failure(self):
        real_capture = cv2.VideoCapture
        resources = []
        class Interrupted:
            def __init__(self, *args):
                self.capture, self.reads, self.released = real_capture(*args), 0, False
                resources.append(self)
            def isOpened(self):
                return self.capture.isOpened()
            def read(self):
                self.reads += 1
                return (False, None) if self.reads == 4 else self.capture.read()
            def release(self):
                self.capture.release()
                self.released = True
        with mock.patch.object(MODULE.cv2, "VideoCapture", Interrupted):
            with self.assertRaisesRegex(ValueError, "incomplete source decode"):
                self.prepare()
        self.assertTrue(resources[0].released)
        self.assertEqual(json.loads((self.output / "summary.json").read_text())["status"], "FAIL")

    def test_diagnostic_selection_does_not_duplicate_uniform_frame(self):
        with mock.patch.dict(MODULE.DIAGNOSTIC_CASES, {"tiny": 2}):
            self.assertEqual(self.prepare()["sample_count"], 4)


if __name__ == "__main__":
    unittest.main()
