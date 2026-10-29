import ctypes
import errno
import mmap
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import cv2
import numpy as np

from rm27_tracking import camera
from rm27_tracking.camera import CameraConfig, CameraSource, V4L2Capture
from rm27_tracking.formats import decode_buffer


class CameraTests(unittest.TestCase):
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
int main(void) { printf("%zu %zu %zu %zu %zu %zu %lu %lu %lu\\n",
sizeof(struct v4l2_capability), sizeof(struct v4l2_format),
sizeof(struct v4l2_requestbuffers), sizeof(struct v4l2_buffer),
sizeof(struct v4l2_streamparm), offsetof(struct v4l2_buffer, timestamp),
(unsigned long)VIDIOC_QUERYCAP, (unsigned long)VIDIOC_DQBUF,
(unsigned long)VIDIOC_STREAMON); }
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
        for values in ({"buffer_count": 1}, {"timeout_s": 0}, {"restart_attempts": -1},
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
