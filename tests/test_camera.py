import ctypes
import errno
import json
import mmap
from pathlib import Path
import shutil
import subprocess
import tempfile
from time import monotonic_ns, sleep
import unittest
from unittest.mock import patch

import cv2
import numpy as np

from rm27_tracking import camera
from rm27_tracking.camera import CameraConfig, CameraSource, V4L2Capture
from rm27_tracking.formats import decode_buffer
from rm27_tracking.recording import RecordingSource, RecordingWriteError, RecordingWriter


class CameraTests(unittest.TestCase):
    def test_capture_finalization_failure_marks_run_and_recording_incomplete(self):
        from contextlib import redirect_stderr
        from io import StringIO
        from rm27_tracking import capture
        from rm27_tracking.pipeline import Frame

        class Frames:
            def __init__(self, config, recorder=None):
                self.recorder = recorder
                self.closed = False

            def __iter__(self):
                for sequence in range(3):
                    if self.closed:
                        return
                    image = np.full((2, 2, 3), sequence, np.uint8)
                    received = monotonic_ns()
                    metadata = {"sequence": sequence, "source_sequence": sequence, "stream_id": 0,
                                "timestamp_ns": (sequence + 1) * 1000, "clock_domain": "v4l2_unknown",
                                "timestamp_semantic": "driver_end_of_frame", "received_monotonic_ns": received,
                                "driver_flags": 0,
                                "format": {"width": 2, "height": 2, "fourcc": "BGR3", "stride": 6, "sizeimage": 12}}
                    self.recorder.append(image.tobytes(), metadata)
                    yield Frame(sequence, image, metadata["timestamp_ns"], metadata["clock_domain"],
                                metadata["timestamp_semantic"], received, source_sequence=sequence)

            def close(self):
                self.closed = True

        finish = RecordingWriter.finish
        def fail_successful_finalization(writer, complete=True, reason="completed"):
            if complete:
                raise RecordingWriteError("injected finalization failure")
            return finish(writer, complete=False, reason=reason)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "config.json"
            config.write_text(json.dumps({"processing": {"max_frames": 1}, "recording": {"max_bytes": 1000}}))
            output = root / "run"
            with patch.object(capture, "CameraSource", Frames), \
                 patch.object(RecordingWriter, "finish", fail_successful_finalization), \
                 redirect_stderr(StringIO()) as stderr:
                with self.assertRaises(SystemExit) as stopped:
                    capture.main(["--config", str(config), "--out", str(output)])
            self.assertEqual(stopped.exception.code, 1)
            self.assertIn("injected finalization failure", stderr.getvalue())
            summary = json.loads((output / "summary.json").read_text())
            self.assertEqual(summary["frame_count"], 1)
            self.assertFalse(summary["passed"])
            self.assertFalse(summary["recording"]["complete"])
            self.assertIn("injected finalization failure", summary["error"])
            manifest = json.loads((output / "recording" / "manifest.json").read_text())
            self.assertFalse(manifest["complete"])
            self.assertEqual(manifest["frame_count"], 1)
            self.assertIn("injected finalization failure", manifest["reason"])

    def test_nv12_nv21_stride_color_and_ownership(self):
        # BT.601 limited-range red: Y=82 U=90 V=240; padding must be ignored.
        nv12 = bytearray([82, 82, 99, 99, 82, 82, 99, 99, 90, 240, 99, 99])
        nv21 = bytearray([82, 82, 99, 99, 82, 82, 99, 99, 240, 90, 99, 99])
        a = decode_buffer(nv12, 2, 2, "NV12", 4)
        b = decode_buffer(nv21, 2, 2, "NV21", 4)
        np.testing.assert_array_equal(a, b)
        np.testing.assert_allclose(a[0, 0], [0, 1, 255], atol=1)
        nv12[:] = bytes(len(nv12))
        np.testing.assert_allclose(a[0, 0], [0, 1, 255], atol=1)
        with self.assertRaises(ValueError):
            decode_buffer(bytes(3), 2, 2, "NV12")
        with self.assertRaises(ValueError):
            decode_buffer(bytes(6), 1, 2, "NV21")

    def test_packed_yuv_rgb_grey_and_jpeg(self):
        for fourcc, raw in [("YUYV", [82, 90, 82, 240]), ("UYVY", [90, 82, 240, 82])]:
            image = decode_buffer(bytes(raw + [11, 11]), 2, 1, fourcc, 6)
            np.testing.assert_allclose(image[0, 0], [0, 1, 255], atol=1)
        np.testing.assert_array_equal(decode_buffer(bytes([1, 2, 3, 99]), 1, 1, "RGB3", 4), [[[3, 2, 1]]])
        np.testing.assert_array_equal(decode_buffer(bytes([1, 2, 3, 99]), 1, 1, "BGR3", 4), [[[1, 2, 3]]])
        np.testing.assert_array_equal(decode_buffer(bytes([50, 99]), 1, 1, "GREY", 2), [[[50, 50, 50]]])
        ok, jpeg = cv2.imencode(".jpg", np.full((4, 4, 3), 100, np.uint8))
        self.assertTrue(ok)
        self.assertEqual(decode_buffer(jpeg, 4, 4, "MJPG").shape, (4, 4, 3))
        with self.assertRaises(ValueError):
            decode_buffer(bytes([0]), 4, 4, "MJPG")
        with self.assertRaises(ValueError):
            decode_buffer(bytes(20), 2, 2, "NM12")

    def test_host_linux_uapi_matches_ctypes(self):
        cc = shutil.which("cc")
        if cc is None:
            self.skipTest("C compiler unavailable; cannot verify host header ABI")
        source = '''#include <stdio.h>
#include <stddef.h>
#include <linux/videodev2.h>
int main(void) { printf("%zu %zu %zu %zu %zu %zu %lu %lu %lu %zu %zu %lu %lu %zu %zu %zu %zu %zu %zu %lu %lu %lu\\n",
sizeof(struct v4l2_capability), sizeof(struct v4l2_format),
sizeof(struct v4l2_requestbuffers), sizeof(struct v4l2_buffer),
sizeof(struct v4l2_streamparm), offsetof(struct v4l2_buffer, timestamp),
(unsigned long)VIDIOC_QUERYCAP, (unsigned long)VIDIOC_DQBUF,
(unsigned long)VIDIOC_STREAMON, sizeof(struct v4l2_queryctrl), sizeof(struct v4l2_control),
(unsigned long)VIDIOC_QUERYCTRL, (unsigned long)VIDIOC_G_CTRL,
sizeof(struct v4l2_plane_pix_format), sizeof(struct v4l2_pix_format_mplane), sizeof(struct v4l2_plane),
offsetof(struct v4l2_pix_format_mplane, plane_fmt), offsetof(struct v4l2_pix_format_mplane, num_planes),
offsetof(struct v4l2_plane, data_offset), (unsigned long)VIDIOC_G_FMT,
(unsigned long)VIDIOC_S_FMT, (unsigned long)VIDIOC_QBUF); }
'''
        with tempfile.TemporaryDirectory() as tmp:
            cfile = Path(tmp) / "abi.c"
            binary = Path(tmp) / "abi"
            cfile.write_text(source)
            subprocess.run([cc, str(cfile), "-o", str(binary)], check=True, capture_output=True)
            actual = list(map(int, subprocess.check_output([str(binary)], text=True).split()))
        expected = [ctypes.sizeof(cls) for cls in (camera.Capability, camera.Format,
                    camera.RequestBuffers, camera.Buffer, camera.StreamParameters)]
        expected += [camera.Buffer.timestamp.offset, camera.ioctl_code(0, camera.Capability, 2),
                     camera.ioctl_code(17, camera.Buffer), camera.ioctl_code(18, ctypes.c_int, 1)]
        expected += [ctypes.sizeof(camera.QueryControl), ctypes.sizeof(camera.Control),
                     camera.ioctl_code(36, camera.QueryControl), camera.ioctl_code(27, camera.Control)]
        expected += [ctypes.sizeof(camera.PlanePixelFormat), ctypes.sizeof(camera.PixelFormatMplane),
                     ctypes.sizeof(camera.Plane), camera.PixelFormatMplane.plane_fmt.offset,
                     camera.PixelFormatMplane.num_planes.offset, camera.Plane.data_offset.offset,
                     camera.ioctl_code(4, camera.Format), camera.ioctl_code(5, camera.Format),
                     camera.ioctl_code(15, camera.Buffer)]
        self.assertEqual(actual, expected)

    def test_kernel_sequence_drops_restart_and_timestamp_provenance(self):
        image = np.zeros((2, 2, 3), np.uint8)
        class FakeCapture:
            count = 0
            instances = []
            def __init__(self, config):
                self.reported = {"buffer_count": config.buffer_count}
                self.stream = FakeCapture.count
                FakeCapture.count += 1
                FakeCapture.instances.append(self)
                self.sequences = iter([2, 4] if not self.stream else [0, 1])
                self.closed = False
            def open(self):
                pass
            def read(self):
                try:
                    seq = next(self.sequences)
                except StopIteration:
                    raise OSError(errno.ENODEV, "removed")
                return image.copy(), seq, seq * 1000, "v4l2_monotonic", "driver_end_of_frame", 123456
            def close(self):
                self.closed = True
        source = CameraSource(CameraConfig(restart_attempts=1, restart_delay_s=0.001))
        with patch.object(camera, "V4L2Capture", FakeCapture):
            frames = iter(source)
            output = [next(frames) for _ in range(4)]
            self.assertEqual([f.sequence for f in output], [0, 1, 2, 3])
            self.assertEqual([f.source_sequence for f in output], [2, 4, 0, 1])
            self.assertEqual([f.stream_id for f in output], [0, 0, 1, 1])
            self.assertEqual(output[0].clock_domain, "v4l2_monotonic")
            self.assertEqual(output[0].timestamp_semantic, "driver_end_of_frame")
            self.assertEqual(source.stats["source_drops"], 1)
            self.assertEqual(source.stats["restarts"], 1)
            with self.assertRaises(RuntimeError):
                next(frames)
        self.assertTrue(source.closed)
        self.assertTrue(all(c.closed for c in FakeCapture.instances))
        self.assertEqual(source.stats["errors"], 2)

    def test_timeout_no_automatic_unbounded_restart(self):
        with patch.object(camera, "V4L2Capture") as factory:
            capture = factory.return_value
            capture.reported = {}
            capture.read.side_effect = TimeoutError("stalled")
            source = CameraSource(CameraConfig())
            factory.assert_not_called()
            with self.assertRaises(RuntimeError):
                next(iter(source))
            self.assertEqual(factory.call_count, 1)
            self.assertTrue(source.closed)
            self.assertEqual(source.stats["restarts"], 0)

    def test_mmap_read_requeues_only_owned_output_and_preserves_metadata(self):
        capture = V4L2Capture(CameraConfig())
        capture.fd = 9
        capture.maps = [bytearray([1, 2, 3, 99])]
        capture.reported = {"width": 1, "height": 1, "fourcc": "BGR3", "stride": 4}
        operations = []
        def ioctl(fd, number, value, direction=3):
            operations.append(number)
            if number == 17:
                value.index, value.bytesused, value.sequence = 0, 4, 7
                value.timestamp.tv_sec, value.timestamp.tv_usec = 5, 10
                value.flags = 0x2000
            if number == 15:
                capture.maps[0][:] = bytes(4)
            return value
        with patch.object(camera, "_ioctl", ioctl), patch.object(camera.select, "select", return_value=([9], [], [])):
            image, seq, stamp, domain, semantic, received = capture.read()
        self.assertEqual(operations, [17, 15])
        np.testing.assert_array_equal(image, [[[1, 2, 3]]])
        self.assertEqual((seq, stamp, domain, semantic), (7, 5_000_010_000, "v4l2_monotonic", "driver_end_of_frame"))
        self.assertGreater(received, 0)
        capture.fd = None
        capture.maps.clear()
        capture.close()
        capture.close()

    def test_raw_recording_precedes_requeue_and_preserves_storage_failure(self):
        capture = V4L2Capture(CameraConfig())
        capture.fd = 9
        capture.maps = [bytearray([1, 2, 3, 99])]
        capture.reported = {'width': 1, 'height': 1, 'fourcc': 'BGR3', 'stride': 4, 'sizeimage': 4}
        operations = []
        def ioctl(fd, number, value, direction=3):
            operations.append(number)
            if number == 17:
                value.bytesused, value.sequence, value.flags = 4, 9, 0x2000
                value.timestamp.tv_usec = 1
            if number == 15:
                capture.maps[0][:] = bytes(4)
            return value
        with tempfile.TemporaryDirectory() as tmp:
            writer = RecordingWriter(Path(tmp)/'recording', 4)
            def record(payload, metadata):
                operations.append('record')
                metadata.update(sequence=0, stream_id=0)
                writer.append(payload, metadata)
            capture.raw_observer = record
            with patch.object(camera, '_ioctl', ioctl), patch.object(camera.select, 'select', return_value=([9], [], [])):
                capture.read()
            writer.finish()
            self.assertEqual(operations, [17, 'record', 15])
            self.assertEqual((writer.path/'payload.bin').read_bytes(), bytes([1, 2, 3, 99]))
            self.assertEqual(list(RecordingSource(writer.path))[0].source_sequence, 9)
        def fail(payload, metadata):
            raise RecordingWriteError('disk full')
        capture.raw_observer = fail
        def disconnected(fd, number, value, direction=3):
            if number == 15:
                raise OSError(errno.ENODEV, 'QBUF removed')
            return ioctl(fd, number, value, direction)
        with patch.object(camera, '_ioctl', disconnected), patch.object(camera.select, 'select', return_value=([9], [], [])):
            with self.assertRaisesRegex(RecordingWriteError, 'disk full'):
                capture.read()
        capture.fd = None
        capture.maps.clear()

    def test_recording_limit_before_queue_eviction_and_qbuf_restart(self):
        from rm27_tracking.pipeline import ProcessingConfig
        from rm27_tracking.runtime import RuntimeConfig, run_pipeline
        class Capture:
            instances = 0
            def __init__(self, config):
                self.epoch = Capture.instances
                Capture.instances += 1
                self.reported = {'width': 2, 'height': 2, 'fourcc': 'BGR3', 'stride': 8, 'sizeimage': 16}
                self.sequence = 0
                self.last_metadata = None
            def open(self):
                if self.epoch == 0:
                    raise OSError(errno.ENODEV, 'initial open failed without frames')
            def read(self):
                sequence = self.sequence
                self.sequence += 1
                received = monotonic_ns()
                self.last_metadata = {'source_sequence': sequence, 'timestamp_ns': sequence*1000,
                                      'received_monotonic_ns': received, 'clock_domain': 'v4l2_unknown',
                                      'timestamp_semantic': 'driver_end_of_frame', 'driver_flags': 0,
                                      'format': self.reported}
                self.raw_observer(bytes([sequence%255])*16, self.last_metadata)
                if self.epoch == 1:
                    raise OSError(errno.ENODEV, 'QBUF failed after recording payload')
                return np.full((2, 2, 3), sequence%255, np.uint8), sequence, sequence*1000, 'v4l2_unknown', 'driver_end_of_frame', received
            def close(self):
                pass
        with tempfile.TemporaryDirectory() as tmp, patch.object(camera, 'V4L2Capture', Capture):
            writer = RecordingWriter(Path(tmp)/'recording', 16*40)
            source = CameraSource(CameraConfig(restart_attempts=2, restart_delay_s=.001), writer)
            def process(image):
                sleep(.002)
                return {'mean': float(image.mean())}
            summary = run_pipeline(source, Path(tmp)/'run', ProcessingConfig(),
                                   RuntimeConfig(queue_capacity=1, queue_policy='latest'), processor=process)
            writer.finish(reason=summary['stop_reason'])
            recorded = list(RecordingSource(writer.path))
            self.assertEqual([frame.sequence for frame in recorded], list(range(40)))
            self.assertEqual([frame.stream_id for frame in recorded[:2]], [1, 2])
            self.assertEqual(summary['stop_reason'], 'recording_byte_limit')
            self.assertTrue(summary['passed'])
            self.assertLess(summary['frame_count'], len(recorded))
            self.assertEqual(summary['capture']['captured'], 39)
            self.assertGreater(summary['capture']['queue_drops'], 0)
            self.assertEqual(summary['capture']['queue_high_water'], 1)
            self.assertTrue(writer.stats['complete'])

    def test_exposure_queries_are_read_only_and_not_frame_synchronized(self):
        operations = []
        def ioctl(fd, number, value, direction=3):
            operations.append(number)
            if value.id == 0x009a0901:
                raise OSError(errno.EINVAL, 'unsupported')
            if number == 36:
                value.name = b'Exposure'
                value.maximum = 1000
            if number == 27:
                value.value = 20
            return value
        with patch.object(camera, '_ioctl', ioctl):
            controls = camera.query_exposure_controls(9)
        self.assertEqual(set(operations), {36, 27})
        self.assertFalse(controls[0]['available'])
        self.assertEqual(controls[1]['units'], '100 microseconds')
        self.assertEqual(controls[-1]['units'], 'driver-defined; physical units unverified')
        for item in controls:
            self.assertGreaterEqual(item['read_end_monotonic_ns'], item['read_start_monotonic_ns'])
            self.assertIn('not frame synchronized', item['provenance'])
    def test_open_stream_and_partial_allocation_cleanup(self):
        for fail_second_buffer in (False, True):
            with self.subTest(partial_failure=fail_second_buffer):
                operations, mappings = [], []
                def ioctl(fd, number, value, direction=3):
                    operations.append(number)
                    if number == 0:
                        value.capabilities = 1 | 0x04000000
                    if number in (4, 5):
                        value.fmt.pix.width, value.fmt.pix.height = 2, 2
                        value.fmt.pix.pixelformat = int.from_bytes(b"BGR3", "little")
                        value.fmt.pix.field, value.fmt.pix.bytesperline = 1, 6
                        value.fmt.pix.sizeimage = 12
                    if number == 9:
                        if fail_second_buffer and value.index == 1:
                            raise OSError(errno.ENOMEM, "allocation failed")
                        value.length = 12
                        value.m.offset = value.index * 4096
                    return value
                # Bind the real mmap function before replacing camera.mmap.mmap.
                original_mmap = mmap.mmap
                def allocate(*args, **kwargs):
                    value = original_mmap(-1, 12)
                    mappings.append(value)
                    return value
                with patch.object(camera, "_ioctl", ioctl), patch.object(camera.os, "open", return_value=9), \
                     patch.object(camera.os, "close") as close, patch.object(camera.mmap, "mmap", allocate):
                    capture = V4L2Capture(CameraConfig(buffer_count=2))
                    if fail_second_buffer:
                        with self.assertRaises(OSError):
                            capture.open()
                        self.assertNotIn(18, operations)
                    else:
                        capture.open()
                        self.assertEqual(capture.reported["buffer_count"], 2)
                        self.assertEqual(operations.count(15), 2)
                        self.assertTrue(capture.streaming)
                        capture.close()
                        self.assertIn(19, operations)
                    close.assert_called_once_with(9)
                    self.assertTrue(all(value.closed for value in mappings))
                    self.assertIsNone(capture.fd)
                    capture.close()

    def test_mplane_open_record_read_and_partial_allocation_cleanup(self):
        for fail_second_buffer in (False, True):
            with self.subTest(partial_failure=fail_second_buffer), tempfile.TemporaryDirectory() as tmp:
                operations, mappings = [], []
                raw = bytes([9, 8, 7, 6, 82, 82, 99, 99, 82, 82, 99, 99, 90, 240, 99, 99])
                original_mmap = mmap.mmap
                def ioctl(fd, number, value, direction=3):
                    operations.append(number)
                    if number == 0:
                        value.capabilities = 0x80000000
                        value.device_caps = 0x1000 | 0x04000000
                    elif number in (4, 5):
                        self.assertEqual(value.type, 9)
                        pix = value.fmt.pix_mp
                        pix.width, pix.height, pix.pixelformat = 2, 2, int.from_bytes(b"NV12", "little")
                        pix.field, pix.num_planes = 1, 1
                        pix.colorspace, pix.ycbcr_enc, pix.quantization = 1, 1, 2
                        pix.plane_fmt[0].bytesperline, pix.plane_fmt[0].sizeimage = 4, 16
                    elif number in (8, 21, 22):
                        self.assertEqual(value.type, 9)
                    elif number in (9, 15, 17):
                        self.assertEqual(value.type, 9)
                        planes = ctypes.cast(value.m.planes, ctypes.POINTER(camera.Plane))
                        self.assertTrue(value.m.planes)
                        if number == 9:
                            self.assertEqual(value.length, 8)
                            if fail_second_buffer and value.index == 1:
                                raise OSError(errno.ENOMEM, "mplane allocation failed")
                            value.length = 1
                            planes[0].length = 16
                            planes[0].m.mem_offset = value.index * 4096
                        elif number == 17:
                            self.assertEqual(value.length, 8)
                            value.length, value.index, value.sequence, value.flags = 1, 0, 12, 0x2000
                            value.timestamp.tv_usec = 12
                            planes[0].length, planes[0].bytesused, planes[0].data_offset = 16, 16, 4
                        else:
                            self.assertEqual(value.length, 1)
                            self.assertEqual(planes[0].length, 16)
                            if len(mappings) == 2:
                                mappings[0][:] = bytes(16)
                    elif number in (18, 19):
                        self.assertEqual(value.value, 9)
                    return value
                def allocate(fd, length, flags, prot, offset):
                    self.assertEqual(length, 16)
                    self.assertEqual(offset, len(mappings) * 4096)
                    mapping = original_mmap(-1, length)
                    mapping[:] = raw
                    mappings.append(mapping)
                    return mapping
                capture = V4L2Capture(CameraConfig(buffer_count=2))
                with patch.object(camera, "_ioctl", ioctl), patch.object(camera.os, "open", return_value=9), \
                     patch.object(camera.os, "close") as close, patch.object(camera.mmap, "mmap", allocate), \
                     patch.object(camera.select, "select", return_value=([9], [], [])):
                    if fail_second_buffer:
                        with self.assertRaisesRegex(OSError, "allocation"):
                            capture.open()
                        self.assertNotIn(18, operations)
                    else:
                        capture.open()
                        self.assertEqual(capture.reported["capture_type"], "mplane")
                        writer = RecordingWriter(Path(tmp) / "recording", 16)
                        def record(payload, metadata):
                            operations.append("record")
                            metadata.update(sequence=0, stream_id=0)
                            writer.append(payload, metadata)
                        capture.raw_observer = record
                        # Initialization's QBUF may replace buffers; restore frame
                        # bytes to model driver filling a queued buffer before DQ.
                        mappings[0][:] = raw
                        image, sequence, stamp, domain, semantic, received = capture.read()
                        writer.finish()
                        self.assertEqual(operations[-3:], [17, "record", 15])
                        self.assertEqual((sequence, stamp, domain), (12, 12000, "v4l2_monotonic"))
                        np.testing.assert_allclose(image[0, 0], [0, 1, 255], atol=1)
                        self.assertEqual((writer.path / "payload.bin").read_bytes(), raw)
                        metadata = json.loads((writer.path / "frames.jsonl").read_text())
                        self.assertEqual(metadata["plane_metadata"], {"length": 16, "bytesused": 16, "data_offset": 4})
                        replayed = list(RecordingSource(writer.path))[0]
                        np.testing.assert_array_equal(replayed.image, image)
                        capture.close()
                        self.assertIn(19, operations)
                    self.assertTrue(all(mapping.closed for mapping in mappings))
                    close.assert_called_once_with(9)
                    self.assertIsNone(capture.fd)

    def test_capture_api_selection_and_rejects_separate_memory_planes(self):
        for requested, capabilities, selected, planes in (("auto", 1 | 0x1000, 1, 1),
                                                         ("mplane", 1 | 0x1000, 9, 1),
                                                         ("single", 0x1000, None, 1),
                                                         ("mplane", 1, None, 1),
                                                         ("auto", 0x1000, 9, 2)):
            with self.subTest(requested=requested, capabilities=capabilities, planes=planes):
                formats = []
                def ioctl(fd, number, value, direction=3):
                    if number == 0:
                        value.capabilities = capabilities | 0x04000000
                    elif number in (4, 5):
                        formats.append(value.type)
                        pix = value.fmt.pix if value.type == 1 else value.fmt.pix_mp
                        pix.width, pix.height, pix.pixelformat, pix.field = 2, 2, int.from_bytes(b"BGR3", "little"), 1
                        if value.type == 9:
                            pix.num_planes = planes
                            pix.plane_fmt[0].bytesperline, pix.plane_fmt[0].sizeimage = 6, 12
                        else:
                            pix.bytesperline, pix.sizeimage = 6, 12
                    elif number == 8:
                        raise OSError(errno.ENOMEM, "stop after successful format selection")
                    return value
                capture = V4L2Capture(CameraConfig(capture_type=requested))
                with patch.object(camera, "_ioctl", ioctl), patch.object(camera.os, "open", return_value=9), \
                     patch.object(camera.os, "close"):
                    if selected is None or planes == 2:
                        with self.assertRaises(ValueError):
                            capture.open()
                    else:
                        with self.assertRaises(OSError):
                            capture.open()
                    self.assertEqual(formats, [] if selected is None else [selected, selected])
                    self.assertIsNone(capture.fd)

    def test_mplane_invalid_plane_metadata_requeues_and_releases_views(self):
        for plane_count, used, data_offset, length in ((2, 16, 4, 16), (1, 17, 4, 16),
                                                     (1, 16, 16, 16), (1, 16, 4, 15),
                                                     (1, 5, 4, 16)):
            with self.subTest(plane_count=plane_count, used=used, data_offset=data_offset, length=length):
                capture = V4L2Capture(CameraConfig())
                capture.fd, capture.buffer_type = 9, 9
                mapping = mmap.mmap(-1, 16)
                capture.maps = [mapping]
                capture.reported = {"width": 2, "height": 2, "fourcc": "NV12", "stride": 4}
                operations = []
                def ioctl(fd, number, value, direction=3):
                    operations.append(number)
                    if number == 17:
                        value.length = plane_count
                        planes = ctypes.cast(value.m.planes, ctypes.POINTER(camera.Plane))
                        planes[0].length, planes[0].bytesused, planes[0].data_offset = length, used, data_offset
                    return value
                with patch.object(camera, "_ioctl", ioctl), patch.object(camera.select, "select", return_value=([9], [], [])), \
                     patch.object(camera.os, "close"):
                    with self.assertRaises(ValueError) as failure:
                        capture.read()
                    self.assertIsNotNone(failure.exception)
                    capture.close()
                self.assertTrue(mapping.closed)
                self.assertEqual(operations, [17, 15])

    def test_decode_failure_releases_mmap_views_before_cleanup(self):
        capture = V4L2Capture(CameraConfig())
        capture.fd = 9
        mapping = mmap.mmap(-1, 4)
        capture.maps = [mapping]
        capture.reported = {"width": 2, "height": 2, "fourcc": "NV12", "stride": 2}
        def ioctl(fd, number, value, direction=3):
            value.bytesused = 4
            return value
        with patch.object(camera, "_ioctl", ioctl), patch.object(camera.select, "select", return_value=([9], [], [])), \
             patch.object(camera.os, "close"):
            with self.assertRaises(ValueError) as failure:
                capture.read()
            # Keep the traceback alive: borrowed NumPy views must already be gone.
            self.assertIsNotNone(failure.exception)
            capture.close()
        self.assertTrue(mapping.closed)

    def test_config_and_failures_requeue(self):
        for values in ({"capture_type": "invalid"}, {"buffer_count": 1}, {"timeout_s": 0}, {"restart_attempts": -1},
                       {"fourcc": "NM12"}, {"width": 1}, {"fps": float("nan")},
                       {"timeout_s": None}, {"restart_delay_s": None}, {"fps": 999999.123456}, {"fps": 1e-20}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                CameraConfig(**values)
        capture = V4L2Capture(CameraConfig())
        capture.fd = 9
        capture.maps = [bytes(4)]
        operations = []
        def ioctl(fd, number, value, direction=3):
            operations.append(number)
            value.flags = 0x40
            return value
        with patch.object(camera, "_ioctl", ioctl), patch.object(camera.select, "select", return_value=([9], [], [])):
            with self.assertRaises(OSError):
                capture.read()
        self.assertEqual(operations, [17, 15])
        capture.fd = None
        capture.maps.clear()


if __name__ == "__main__":
    unittest.main()
