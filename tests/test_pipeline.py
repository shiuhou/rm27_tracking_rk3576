import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import cv2
import numpy as np

from rm27_tracking.pipeline import ProcessingConfig, ReplaySource, preprocess
from rm27_tracking.replay import run


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.images = self.root / "images"
        self.images.mkdir()
        # Deliberately create in reverse order to check replay sorting.
        for index, color in [(1, (0, 0, 255)), (0, (255, 0, 0))]:
            image = np.full((24, 32, 3), color, np.uint8)
            self.assertTrue(cv2.imwrite(str(self.images / f"{index:03}.png"), image))

    def test_replay_order_clock_and_owned_preprocessing(self):
        source = ReplaySource(self.images, 20)
        frames = list(source)
        self.assertTrue(source.closed)
        self.assertEqual([f.timestamp_ns for f in frames], [0, 50_000_000])
        self.assertEqual(frames[0].image[0, 0].tolist(), [255, 0, 0])
        self.assertEqual(frames[1].timestamp_semantic, "synthetic_sequence_fps")
        self.assertGreaterEqual(frames[1].received_monotonic_ns, frames[0].received_monotonic_ns)
        output = preprocess(frames[1].image, ProcessingConfig(width=8, height=6, grayscale=True))
        self.assertEqual(output.shape, (6, 8))
        self.assertEqual(int(output[0, 0]), 76)  # known BGR red to gray
        output[:] = 0
        self.assertEqual(frames[1].image[0, 0].tolist(), [0, 0, 255])
        with self.assertRaises(RuntimeError):
            list(source)

    def test_reproducible_logs_and_limit(self):
        hashes = []
        for name in ("a", "b"):
            result = run(self.images, self.root / name, ProcessingConfig(max_frames=1))
            self.assertTrue(result["passed"])
            self.assertEqual(result["stop_reason"], "max_frames")
            rows = [json.loads(line) for line in (self.root / name / "frames.jsonl").read_text().splitlines()]
            self.assertEqual(len(rows), 1)
            hashes.append(rows[0]["processed_sha256"])
            self.assertEqual(len((self.root / name / "inputs.jsonl").read_text().splitlines()), 2)
        self.assertEqual(*hashes)
        with self.assertRaises(FileExistsError):
            run(self.images, self.root / "a", ProcessingConfig())
        with self.assertRaises(ValueError):
            run(self.images, self.images / "output", ProcessingConfig())

    def test_frame_limit_does_not_decode_corrupt_tail(self):
        (self.images / '001.png').write_bytes(b'corrupt beyond requested prefix')
        result = run(self.images, self.root / 'prefix', ProcessingConfig(max_frames=1))
        self.assertTrue(result['passed'])
        self.assertEqual(result['frame_count'], 1)
        self.assertEqual(result['capture']['captured'], 1)

    def test_real_video_cli(self):
        path = self.root / "sample.avi"
        writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"MJPG"), 25, (32, 24))
        self.assertTrue(writer.isOpened(), "host requires an MJPG encoder for this fixture")
        try:
            for value in (30, 100, 200):
                writer.write(np.full((24, 32, 3), value, np.uint8))
        finally:
            writer.release()
        config = self.root / "config.json"
        config.write_text(json.dumps({"fps": 25, "grayscale": True}))
        result = subprocess.run([sys.executable, "-m", "rm27_tracking.replay", str(path),
                                 "--out", str(self.root / "video"), "--config", str(config)],
                                text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        summary = json.loads(result.stdout)
        self.assertTrue(summary["passed"])
        self.assertEqual(summary["frame_count"], 3)
        rows = [json.loads(line) for line in (self.root / "video/frames.jsonl").read_text().splitlines()]
        self.assertEqual([r["timestamp_ns"] for r in rows], [0, 40_000_000, 80_000_000])
        self.assertEqual(rows[0]["output_shape"], [24, 32])

    def test_bad_input_and_configuration(self):
        for kwargs in ({"fps": 0}, {"fps": float("nan")}, {"fps": True}, {"width": 10},
                       {"max_frames": -1}, {"max_frames": 1.5}, {"grayscale": "false"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                ProcessingConfig(**kwargs)
        with self.assertRaises(ValueError):
            ReplaySource(self.root / "missing", 30)
        (self.images / "002.png").write_bytes(b"broken")
        with self.assertRaises(ValueError):
            run(self.images, self.root / "failed", ProcessingConfig())
        summary = json.loads((self.root / "failed/summary.json").read_text())
        self.assertFalse(summary["passed"])
        self.assertEqual(summary["frame_count"], 2)
        with self.assertRaises(ValueError):
            preprocess(np.zeros((2, 2), np.uint8), ProcessingConfig())



if __name__ == "__main__":
    unittest.main()
