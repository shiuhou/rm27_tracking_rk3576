import hashlib
import json
from pathlib import Path
import tempfile
import tracemalloc
import unittest

import cv2
import numpy as np

from rm27_tracking.formats import decode_buffer
from rm27_tracking.recording import RecordingLimit, RecordingSource, RecordingWriteError, RecordingWriter


class RecordingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def metadata(self, sequence=0, source_sequence=None, stream_id=0, stamp=None, fmt=None):
        return {"sequence": sequence, "source_sequence": sequence if source_sequence is None else source_sequence,
                "stream_id": stream_id, "timestamp_ns": (sequence + 1) * 1000 if stamp is None else stamp,
                "clock_domain": "v4l2_unknown", "timestamp_semantic": "driver_end_of_frame",
                "received_monotonic_ns": 9_000_000_000_000 + sequence * 10000, "driver_flags": 0,
                "format": fmt or {"width": 2, "height": 2, "stride": 8, "fourcc": "BGR3", "sizeimage": 16},
                "exposure_controls": {"available": False, "provenance": "readback_not_frame_synchronized"}}

    def record(self, count=3):
        path = self.root / "recording"
        writer = RecordingWriter(path, 1000)
        for sequence in range(count):
            writer.append(bytes([sequence + 1]) * 16, self.metadata(sequence))
        writer.finish()
        return path

    def rows(self, path):
        return [json.loads(line) for line in (path / "frames.jsonl").read_text().splitlines()]

    def write_rows(self, path, rows):
        (path / "frames.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))

    def incomplete(self, path):
        file = path / "manifest.json"
        manifest = json.loads(file.read_text())
        manifest.update(complete=False, reason="interrupted")
        file.write_text(json.dumps(manifest))

    def test_exact_padded_yuv_and_mjpeg_payload_round_trip(self):
        for code in ("NV12", "NV21", "YUYV", "UYVY", "MJPG"):
            with self.subTest(code=code):
                width, height = 4, 4
                if code == "MJPG":
                    image = np.full((height, width, 3), (20, 80, 170), np.uint8)
                    ok, encoded = cv2.imencode(".jpg", image)
                    self.assertTrue(ok)
                    payload = encoded.tobytes() + b"exact-trailing-driver-bytes"
                    stride = 0
                elif code in {"NV12", "NV21"}:
                    stride = 8
                    raw = np.full((height * 3 // 2, stride), 237, np.uint8)
                    raw[:height, :width] = 80
                    raw[height:, :width] = np.tile([90, 190] if code == "NV12" else [190, 90], width // 2)
                    payload = raw.tobytes()
                else:
                    stride = 12
                    raw = np.full((height, stride), 237, np.uint8)
                    raw[:, :width * 2] = np.tile([80, 90, 80, 190] if code == "YUYV" else [90, 80, 190, 80], width // 2)
                    payload = raw.tobytes()
                expected = decode_buffer(payload, width, height, code, stride)
                fmt = {"width": width, "height": height, "stride": stride, "fourcc": code,
                       "sizeimage": len(payload), "colorspace": 1, "ycbcr_enc": 1, "quantization": 2}
                metadata = self.metadata(fmt=fmt)
                path = self.root / code
                writer = RecordingWriter(path, len(payload))
                row = writer.append(memoryview(payload), metadata)
                writer.finish()
                self.assertEqual((path / "payload.bin").read_bytes(), payload)
                self.assertEqual(row["sha256"], hashlib.sha256(payload).hexdigest())
                self.assertEqual(row["bytesused"], len(payload))
                self.assertEqual(row["payload_offset"], 0)
                frame = next(iter(RecordingSource(path / "manifest.json")))
                np.testing.assert_array_equal(frame.image, expected)
                self.assertTrue(frame.image.flags.owndata)
                for key, value in metadata.items():
                    self.assertEqual(frame.acquisition_metadata[key], value)
                self.assertEqual(frame.received_monotonic_ns, metadata["received_monotonic_ns"])
                self.assertEqual(frame.original_received_monotonic_ns, metadata["received_monotonic_ns"])
                self.assertIsInstance(frame.replay_received_monotonic_ns, int)
                self.assertNotEqual(frame.replay_received_monotonic_ns, frame.received_monotonic_ns)
                self.assertTrue(writer.stats["complete"])
                self.assertGreater(writer.stats["write_total_ns"], 0)

    def test_payload_byte_limit_uses_nbytes_for_multidimensional_buffers(self):
        payload = np.arange(16, dtype=np.uint8).reshape(2, 8)
        writer = RecordingWriter(self.root/'array', 16)
        try:
            with self.assertRaisesRegex(ValueError, 'contiguous'):
                writer.append(payload[:, ::2], self.metadata())
            row = writer.append(memoryview(payload), self.metadata())
            self.assertEqual(row['bytesused'], 16)
            with self.assertRaises(RecordingLimit):
                writer.append(memoryview(payload), self.metadata(1))
        finally:
            writer.finish()
        self.assertEqual((writer.path/'payload.bin').read_bytes(), payload.tobytes())
        self.assertEqual(len(list(RecordingSource(writer.path))), 1)

    def test_mplane_payload_offset_preserves_original_prefix_and_bounds(self):
        path = self.root/'mplane'
        payload = b'prefix' + bytes([1, 2, 3, 99, 99, 99])
        metadata = self.metadata(fmt={'width': 1, 'height': 1, 'fourcc': 'BGR3', 'stride': 6,
                                      'sizeimage': len(payload), 'num_planes': 1, 'capture_type': 'mplane'})
        metadata['plane_metadata'] = {'data_offset': 6, 'length': 64, 'bytesused': len(payload)}
        writer = RecordingWriter(path, len(payload))
        try:
            writer.append(payload, metadata)
        finally:
            writer.finish()
        self.assertEqual((path/'payload.bin').read_bytes(), payload)
        frame = list(RecordingSource(path))[0]
        np.testing.assert_array_equal(frame.image, [[[1, 2, 3]]])
        self.assertEqual(frame.acquisition_metadata['plane_metadata'], metadata['plane_metadata'])
        original = self.rows(path)
        for bad in ('missing', None):
            rows = json.loads(json.dumps(original))
            if bad == 'missing':
                rows[0].pop('plane_metadata')
            else:
                rows[0]['plane_metadata'] = bad
            self.write_rows(path, rows)
            with self.subTest(plane_metadata=bad), self.assertRaisesRegex(ValueError, 'plane_metadata'):
                list(RecordingSource(path))
        for bad in ({'data_offset': len(payload), 'length': 64, 'bytesused': len(payload)},
                    {'data_offset': -1, 'length': 64, 'bytesused': len(payload)},
                    {'data_offset': 6, 'length': 2, 'bytesused': len(payload)},
                    {'data_offset': 6, 'length': 64, 'bytesused': 3}):
            rows = json.loads(json.dumps(original))
            rows[0]['plane_metadata'] = bad
            self.write_rows(path, rows)
            with self.subTest(bad=bad), self.assertRaisesRegex(ValueError, 'plane'):
                list(RecordingSource(path))

    def test_single_plane_legacy_replay_rejects_explicit_null_layout(self):
        path = self.record(1)
        self.assertEqual(len(list(RecordingSource(path))), 1)
        rows = self.rows(path)
        rows[0]['plane_metadata'] = None
        self.write_rows(path, rows)
        with self.assertRaisesRegex(ValueError, 'plane_metadata'):
            list(RecordingSource(path))

    def test_restart_sequence_gaps_and_uint32_wrap(self):
        path = self.root / "epochs"
        writer = RecordingWriter(path, 1000)
        for app, source, epoch, stamp in ((0, 0xfffffffe, 0, 10), (1, 1, 0, 20), (3, 0, 1, 0), (4, 1, 1, 1)):
            writer.append(bytes(16), self.metadata(app, source, epoch, stamp))
        writer.finish()
        source = RecordingSource(path)
        frames = list(source)
        self.assertEqual([f.source_sequence for f in frames], [0xfffffffe, 1, 0, 1])
        self.assertEqual([f.stream_id for f in frames], [0, 0, 1, 1])
        self.assertEqual(source.stats["source_drops"], 2)
        self.assertEqual(source.stats["application_sequence_gaps"], 1)
        self.assertEqual(source.stats["streams"], 2)
        self.assertEqual(source.stats["verified_payload_bytes"], 64)

    def test_empty_restart_epochs_are_preserved(self):
        path = self.root / "empty_epochs"
        writer = RecordingWriter(path, 1000)
        for app, epoch in ((0, 2), (1, 5)):
            writer.append(bytes(16), self.metadata(app, source_sequence=0, stream_id=epoch, stamp=0))
        writer.finish()
        source = RecordingSource(path)
        self.assertEqual([frame.stream_id for frame in source], [2, 5])
        self.assertEqual(source.stats["empty_stream_epochs"], 4)
        rows = self.rows(path)
        rows[1]["stream_id"] = 1
        self.write_rows(path, rows)
        with self.assertRaisesRegex(ValueError, "stream transition"):
            list(RecordingSource(path))

    def test_colorimetry_and_frame_allocation_bounds(self):
        path = self.record(1)
        rows = self.rows(path)
        with self.assertRaisesRegex(ValueError, "memory limit"):
            list(RecordingSource(path, max_frame_bytes=15))
        self.assertEqual(len(list(RecordingSource(path, max_frame_bytes=16))), 1)
        rows[0]["format"].update(fourcc="NV12", colorspace=3, ycbcr_enc=2, quantization=2)
        self.write_rows(path, rows)
        with self.assertRaisesRegex(ValueError, "colorimetry"):
            list(RecordingSource(path))
        rows[0]["format"].update(fourcc="MJPG", width=65535, height=65535)
        self.write_rows(path, rows)
        with self.assertRaisesRegex(ValueError, "memory limit"):
            list(RecordingSource(path))
        rows[0]["format"].pop("sizeimage")
        self.write_rows(path, rows)
        with self.assertRaisesRegex(ValueError, "sizeimage"):
            list(RecordingSource(path))

    def test_corrupt_hash_offsets_ordering_and_stream_transitions(self):
        path = self.record()
        original = self.rows(path)
        changes = (("sha256", "0" * 64), ("payload_offset", 0), ("sequence", 0),
                   ("source_sequence", 0), ("source_sequence", 0xffffffff), ("stream_id", -1),
                   ("timestamp_ns", 0), ("received_monotonic_ns", 0),
                   ("clock_domain", "different_clock"))
        for key, value in changes:
            with self.subTest(key=key, value=value):
                rows = json.loads(json.dumps(original))
                rows[1][key] = value
                self.write_rows(path, rows)
                for allow in (False, True):
                    with self.assertRaises(ValueError):
                        list(RecordingSource(path, allow_incomplete=allow))
        self.write_rows(path, original)
        payload = bytearray((path / "payload.bin").read_bytes())
        payload[0] ^= 1
        (path / "payload.bin").write_bytes(payload)
        self.incomplete(path)
        with self.assertRaisesRegex(ValueError, "hash"):
            list(RecordingSource(path, allow_incomplete=True))

    def test_incomplete_prefix_requires_opt_in_and_complete_truncation_rejected(self):
        path = self.record()
        (path / "payload.bin").write_bytes((path / "payload.bin").read_bytes()[:-3])
        with self.assertRaisesRegex(ValueError, "truncated"):
            list(RecordingSource(path))
        self.incomplete(path)
        with self.assertRaisesRegex(ValueError, "incomplete"):
            RecordingSource(path)
        source = RecordingSource(path, allow_incomplete=True)
        self.assertEqual([f.sequence for f in source], [0, 1])
        self.assertTrue(source.stats["incomplete_tail"])

    def test_partial_index_tail_and_complete_invalid_json(self):
        path = self.record(2)
        self.incomplete(path)
        with (path / "frames.jsonl").open("ab") as stream:
            stream.write(b'{"sequence":2')
        source = RecordingSource(path, allow_incomplete=True)
        self.assertEqual(len(list(source)), 2)
        self.assertTrue(source.stats["incomplete_tail"])
        with (path / "frames.jsonl").open("ab") as stream:
            stream.write(b"\n")
        with self.assertRaisesRegex(ValueError, "JSON"):
            list(RecordingSource(path, allow_incomplete=True))

    def test_complete_manifest_counts_and_trailing_payload_are_verified(self):
        path = self.record(2)
        with (path / "payload.bin").open("ab") as stream:
            stream.write(b"unindexed bytes")
        with self.assertRaisesRegex(ValueError, "counts|size"):
            list(RecordingSource(path))
        self.incomplete(path)
        source = RecordingSource(path, allow_incomplete=True)
        self.assertEqual(len(list(source)), 2)
        self.assertTrue(source.stats["incomplete_tail"])

    def test_storage_limit_and_exclusive_directory(self):
        for invalid in (0, -1, True, 1.5):
            with self.assertRaises(ValueError):
                RecordingWriter(self.root / "invalid", invalid)
        path = self.root / "limited"
        writer = RecordingWriter(path, 31)
        writer.append(bytes(16), self.metadata())
        with self.assertRaises(RecordingLimit):
            writer.append(bytes(16), self.metadata(1))
        writer.finish(reason="byte_limit")
        self.assertEqual(writer.stats["frame_count"], 1)
        self.assertEqual((path / "payload.bin").stat().st_size, 16)
        self.assertEqual(len(list(RecordingSource(path))), 1)
        with self.assertRaises(FileExistsError):
            RecordingWriter(path, 1000)

    def test_write_failure_retains_only_fully_written_records(self):
        for failed_stream in ("payload", "index"):
            with self.subTest(stream=failed_stream):
                path = self.root / failed_stream
                writer = RecordingWriter(path, 1000)
                writer.append(bytes(16), self.metadata())
                original = getattr(writer, failed_stream)
                class FailedWrite:
                    def write(self, data):
                        original.write(data[:3])
                        raise OSError("storage unavailable")
                    def close(self):
                        original.close()
                setattr(writer, failed_stream, FailedWrite())
                with self.assertRaisesRegex(RecordingWriteError, "storage"):
                    writer.append(bytes(16), self.metadata(1))
                self.assertFalse(writer.stats["complete"])
                self.assertEqual(writer.stats["frame_count"], 1)
                self.assertFalse(json.loads((path / "manifest.json").read_text())["complete"])
                source = RecordingSource(path, allow_incomplete=True)
                self.assertEqual([f.sequence for f in source], [0])
                self.assertTrue(source.stats["incomplete_tail"])

    def test_truncated_payload_before_more_records_is_corruption(self):
        path = self.record(3)
        self.incomplete(path)
        (path / "payload.bin").write_bytes(bytes(20))
        # Existing prefix also has a deliberately changed first hash; restore it
        # so the test reaches the nonfinal truncated second record.
        rows = self.rows(path)
        rows[0]["sha256"] = hashlib.sha256(bytes(16)).hexdigest()
        self.write_rows(path, rows)
        with self.assertRaisesRegex(ValueError, "further index"):
            list(RecordingSource(path, allow_incomplete=True))

    def test_returned_metadata_cannot_mutate_ordering_and_invalid_append_is_retryable(self):
        path = self.root / "snapshot"
        writer = RecordingWriter(path, 1000)
        row = writer.append(bytes(16), self.metadata())
        row["source_sequence"] = 1000
        row["format"]["width"] = 600
        with self.assertRaisesRegex(ValueError, "source sequence"):
            writer.append(bytes(16), self.metadata(1, source_sequence=0))
        writer.append(bytes(16), self.metadata(1))
        writer.finish()
        self.assertEqual(len(list(RecordingSource(path))), 2)

    def test_finalization_failure_keeps_incomplete_manifest(self):
        from unittest.mock import patch
        path = self.root / "finalization"
        writer = RecordingWriter(path, 1000)
        writer.append(bytes(16), self.metadata())
        with patch.object(Path, "replace", side_effect=OSError("rename failed")):
            with self.assertRaisesRegex(RecordingWriteError, "finalization"):
                writer.finish()
        self.assertFalse(writer.stats["complete"])
        self.assertFalse(json.loads((path / "manifest.json").read_text())["complete"])
        self.assertEqual(len(list(RecordingSource(path, allow_incomplete=True))), 1)

    def test_streaming_memory_does_not_grow_with_frame_count(self):
        path = self.root / "bounded"
        tracemalloc.start()
        try:
            writer = RecordingWriter(path, 5000 * 16)
            for index in range(5000):
                writer.append(bytes(16), self.metadata(index))
            writer.finish()
            _, writer_peak = tracemalloc.get_traced_memory()
            tracemalloc.reset_peak()
            count = 0
            for frame in RecordingSource(path):
                count += 1
            _, reader_peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertEqual(count, 5000)
        self.assertLess(writer_peak, 1024 * 1024)
        self.assertLess(reader_peak, 1024 * 1024)


if __name__ == "__main__":
    unittest.main()
