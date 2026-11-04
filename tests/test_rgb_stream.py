"""Real OpenCV RGB -> original confirmed measurement host transport tests."""
import argparse
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

import cv2
import numpy as np


ROOT = Path(__file__).resolve().parents[1]
BINARY = ROOT / "build/video-replay/dart_rgb_stream"
CONFIG = ROOT / "tools/video_replay/simulation_green.conf"


def image(x=320, y=240, color=(10, 235, 10)):
    frame = np.full((480, 640, 3), 20, dtype=np.uint8)
    cv2.circle(frame, (x, y), 6, color, -1)
    cv2.circle(frame, (x, y), 1, (245, 245, 245), -1)
    return frame


def header(sequence=0, timestamp=0, epoch=0, **overrides):
    result = dict(protocol="rm27-rgb-v1", stream_epoch=epoch,
                  source_sequence=sequence, simulation_timestamp_us=timestamp,
                  width=640, height=480, stride=1920)
    result.update(overrides)
    return result


def packet(frame=None, **identity):
    return json.dumps(header(**identity), separators=(",", ":")).encode() + b"\n" + (
        image() if frame is None else frame).tobytes()


class RgbStreamTests(unittest.TestCase):
    def run_stream(self, data, config=CONFIG, success=True):
        result = subprocess.run([str(BINARY), "--config", str(config)],
                                input=data, capture_output=True, timeout=20)
        if success:
            self.assertEqual(result.returncode, 0, result.stderr.decode())
            self.assertEqual(result.stderr, b"")
        else:
            self.assertNotEqual(result.returncode, 0)
            self.assertTrue(result.stderr.startswith(b"dart_rgb_stream:"), result.stderr)
        return [json.loads(line) for line in result.stdout.splitlines()]

    def test_actual_confirmed_rgb_measurement_orientation_identity_and_clocks(self):
        frames = [packet(image(375, 190), sequence=n, timestamp=n * 100000)
                  for n in range(8)]
        rows = self.run_stream(b"".join(frames))
        self.assertEqual(len(rows), 8)
        self.assertFalse(rows[0]["pixel_measurement"]["valid"])
        self.assertEqual(rows[0]["timestamp_us"], 0)
        self.assertTrue(any(row["pixel_measurement"]["valid"] for row in rows))
        for index, row in enumerate(rows):
            transport = row["rgb_stream"]
            self.assertEqual((transport["source_sequence"], transport["stream_epoch"]), (index, 0))
            self.assertEqual(transport["simulation_timestamp_us"], index * 100000)
            self.assertEqual((transport["width"], transport["height"], transport["stride"]),
                             (640, 480, 1920))
            self.assertEqual(transport["source_kind"], "simulation")
            self.assertLessEqual(transport["host_header_received_us"], transport["host_payload_received_us"])
            self.assertLessEqual(transport["host_payload_received_us"], transport["host_processing_start_us"])
            self.assertLessEqual(transport["host_processing_start_us"], transport["host_processing_end_us"])
            self.assertEqual(row["source_received_us"], 0)
            self.assertFalse(row["safe_for_control"])
            self.assertFalse(row["angles_valid"])
            self.assertFalse(row["pose"]["valid"])
            measurement = row["pixel_measurement"]
            self.assertFalse(measurement["position_covariance_valid"])
            self.assertIsNone(measurement["position_covariance_px2"])
            self.assertFalse(measurement["exposure_timestamp_valid"])
            if measurement["valid"]:
                self.assertTrue(measurement["tracker_confirmed"])
                self.assertTrue(measurement["new_measurement"])
                self.assertEqual(measurement["selected_count"], 1)
                self.assertAlmostEqual(measurement["center"]["x"], 375, delta=1)
                self.assertAlmostEqual(measurement["center"]["y"], 190, delta=1)
                self.assertEqual((measurement["source_width"], measurement["source_height"]), (640, 480))
                self.assertEqual(measurement["source_sequence"], index)
                self.assertEqual(measurement["source_pts_raw"], index * 100000)
                self.assertEqual(measurement["timestamp_source"], "simulation_time_us")

    def test_missing_image_is_not_a_confirmed_measurement_and_rgb_channels_are_real(self):
        for color in [(235, 10, 10), (10, 10, 235)]:
            rows = self.run_stream(b"".join(packet(image(color=color), sequence=n,
                                                    timestamp=n * 100000) for n in range(6)))
            self.assertTrue(all(not row["pixel_measurement"]["valid"] for row in rows))
        frames = [packet(sequence=n, timestamp=n * 100000) for n in range(5)]
        frames.append(packet(np.full((480, 640, 3), 20, np.uint8), sequence=5, timestamp=500000))
        rows = self.run_stream(b"".join(frames))
        self.assertTrue(rows[-2]["pixel_measurement"]["valid"])
        self.assertFalse(rows[-1]["pixel_measurement"]["valid"])

    def test_restart_rebuilds_confirmation_and_counts_real_sequence_gaps(self):
        requests = [packet(sequence=n, timestamp=n * 100000) for n in range(5)]
        requests += [packet(sequence=7, timestamp=700000), packet(sequence=0, timestamp=0, epoch=1)]
        rows = self.run_stream(b"".join(requests))
        self.assertTrue(rows[-2]["pixel_measurement"]["valid"])
        self.assertEqual(rows[-2]["rgb_stream"]["sequence_gap"], 2)
        self.assertEqual(rows[-2]["upstream_missing"], 2)
        self.assertEqual(rows[-1]["rgb_stream"]["stream_epoch"], 1)
        self.assertEqual(rows[-1]["upstream_missing"], 0)
        self.assertFalse(rows[-1]["pixel_measurement"]["valid"])

    def test_duplicate_reversed_time_and_epoch_are_fatal_without_fresh_response(self):
        first = packet(sequence=5, timestamp=500000, epoch=1)
        for request in [packet(sequence=5, timestamp=600000, epoch=1),
                        packet(sequence=4, timestamp=600000, epoch=1),
                        packet(sequence=6, timestamp=500000, epoch=1),
                        packet(sequence=6, timestamp=400000, epoch=1),
                        packet(sequence=6, timestamp=600000, epoch=0)]:
            with self.subTest(header=request[:180]):
                self.assertEqual(len(self.run_stream(first + request, success=False)), 1)

    def test_limits_invalid_headers_config_and_partial_requests_fail(self):
        bad_headers = [header(stride=1921), header(width=0), header(width=4097),
                       header(height=4096, width=4096, stride=12288),
                       header(protocol="other"), header(source_sequence=True),
                       header(source_sequence="1"), header(source_sequence=1.5),
                       header(source_sequence=-1), header(source_sequence=2**53),
                       header(unexpected=1)]
        for request in bad_headers:
            with self.subTest(request=request):
                self.assertEqual(self.run_stream(json.dumps(request).encode() + b"\n", success=False), [])
        for data in [b"{", b"\n", b"x" * 4097, b"{}\n", b"[]\n",
                     json.dumps(header()).encode() + b"\n" + b"short body"]:
            self.assertEqual(self.run_stream(data, success=False), [])
        valid = json.dumps(header())
        for malformed in [valid + " trailing", valid + "{}", valid + ",",
                          valid.replace('"source_sequence": 0', '"source_sequence": 01'),
                          valid.replace('"source_sequence": 0', '"source_sequence": 0e0'),
                          valid.replace('"source_sequence": 0', '"source_sequence": 0.0'),
                          valid.replace('"source_sequence": 0', '"source_sequence": 0,"source_sequence":0')]:
            self.assertEqual(self.run_stream(malformed.encode() + b"\n", success=False), [])
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "invalid.conf"
            for addition in ["npu.enabled=true", "target_geometry.pose_enabled=true",
                             "camera.width=0", "tracking.confirm_hits=99"]:
                path.write_text(CONFIG.read_text() + "\n" + addition + "\n")
                self.assertEqual(self.run_stream(b"", config=path, success=False), [])

    def test_uint64_json_integer_identity_remains_exact_beyond_signed_32_bits(self):
        for number in [2**31, 2**32 + 7, 2**53 - 1]:
            with self.subTest(number=number):
                rows = self.run_stream(packet(sequence=number, timestamp=number))
                self.assertEqual(rows[0]["rgb_stream"]["source_sequence"], number)
                self.assertEqual(rows[0]["timestamp_us"], number)

    def test_persistent_request_reply_short_writes_and_clean_eof(self):
        process = subprocess.Popen([str(BINARY), "--config", str(CONFIG)],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE)
        try:
            for number in range(12):
                data = packet(sequence=number, timestamp=number * 100000)
                # The binary must fill one frame even when a pipe read is short.
                for offset in range(0, len(data), 7001):
                    process.stdin.write(data[offset:offset + 7001])
                    process.stdin.flush()
                row = json.loads(process.stdout.readline())
                self.assertEqual(row["rgb_stream"]["source_sequence"], number)
            process.stdin.close()
            self.assertEqual(process.wait(timeout=10), 0)
            self.assertEqual(process.stdout.read(), b"")
            self.assertEqual(process.stderr.read(), b"")
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            process.stdout.close()
            process.stderr.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary", type=Path, default=BINARY)
    arguments, remaining = parser.parse_known_args()
    BINARY = arguments.binary.resolve()
    unittest.main(argv=[__file__, *remaining])
