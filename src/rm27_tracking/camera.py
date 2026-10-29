"""Linux V4L2 single-planar MMAP acquisition, with explicit kernel metadata.

ABI: Linux UAPI videodev2.h on LP64 x86_64/aarch64. No device is opened until
iteration. No exposure/synchronization qualification is implied by buffer flags.
Reference: kernel.org/doc/html/latest/userspace-api/media/v4l/buffer.html
"""
import ctypes as C
from dataclasses import dataclass
import errno
from fractions import Fraction
import fcntl
import math
import mmap
import os
import platform
import select
import threading
from time import monotonic, monotonic_ns

from .formats import SUPPORTED_FORMATS, decode_buffer

U32 = C.c_uint32


class Capability(C.Structure):
    _fields_ = [("driver", C.c_char * 16), ("card", C.c_char * 32), ("bus_info", C.c_char * 32),
                ("version", U32), ("capabilities", U32), ("device_caps", U32), ("reserved", U32 * 3)]


class PixelFormat(C.Structure):
    _fields_ = [(name, U32) for name in ("width", "height", "pixelformat", "field", "bytesperline",
               "sizeimage", "colorspace", "priv", "flags", "ycbcr_enc", "quantization", "xfer_func")]


class FormatUnion(C.Union):
    _fields_ = [("pix", PixelFormat), ("raw", C.c_uint8 * 200), ("alignment", C.c_void_p)]


class Format(C.Structure):
    _fields_ = [("type", U32), ("fmt", FormatUnion)]


class RequestBuffers(C.Structure):
    _fields_ = [("count", U32), ("type", U32), ("memory", U32), ("capabilities", U32),
                ("flags", C.c_uint8), ("reserved", C.c_uint8 * 3)]


class Timeval(C.Structure):
    _fields_ = [("tv_sec", C.c_long), ("tv_usec", C.c_long)]


class Timecode(C.Structure):
    _fields_ = [("type", U32), ("flags", U32), ("values", C.c_uint8 * 8)]


class BufferMemory(C.Union):
    _fields_ = [("offset", U32), ("userptr", C.c_ulong), ("planes", C.c_void_p), ("fd", C.c_int32)]


class Buffer(C.Structure):
    _fields_ = [("index", U32), ("type", U32), ("bytesused", U32), ("flags", U32), ("field", U32),
                ("timestamp", Timeval), ("timecode", Timecode), ("sequence", U32), ("memory", U32),
                ("m", BufferMemory), ("length", U32), ("reserved2", U32), ("request_fd", C.c_int32)]


class FractionU32(C.Structure):
    _fields_ = [("numerator", U32), ("denominator", U32)]


class CaptureParameters(C.Structure):
    _fields_ = [("capability", U32), ("capturemode", U32), ("timeperframe", FractionU32),
                ("extendedmode", U32), ("readbuffers", U32), ("reserved", U32 * 4)]


class ParametersUnion(C.Union):
    _fields_ = [("capture", CaptureParameters), ("raw", C.c_uint8 * 200)]


class StreamParameters(C.Structure):
    _fields_ = [("type", U32), ("parm", ParametersUnion)]


def ioctl_code(number, structure, direction=3):
    return (direction << 30) | (C.sizeof(structure) << 16) | (ord("V") << 8) | number


def _ioctl(fd, number, value, direction=3):
    # bytearray handles mutable buffers consistently across Python versions.
    data = bytearray(C.string_at(C.addressof(value), C.sizeof(value)))
    fcntl.ioctl(fd, ioctl_code(number, type(value), direction), data, True)
    C.memmove(C.addressof(value), bytes(data), len(data))
    return value


def _fourcc(text):
    return int.from_bytes(text.encode("ascii"), "little")


@dataclass(frozen=True)
class CameraConfig:
    device: str = "/dev/video0"
    backend: str = "v4l2"
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    fourcc: str | None = None
    buffer_count: int = 4
    timeout_s: float = 2.0
    restart_attempts: int = 0
    restart_delay_s: float = 0.25

    def __post_init__(self):
        if not isinstance(self.device, str) or not self.device.startswith("/dev/video"):
            raise ValueError("device must be an explicit /dev/video path")
        if self.backend != "v4l2":
            raise ValueError("only direct single-planar v4l2 is implemented")
        if (self.width is None) != (self.height is None):
            raise ValueError("width and height must be specified together")
        for name in ("width", "height"):
            value = getattr(self, name)
            if value is not None and (type(value) is not int or not 0 < value <= 65535):
                raise ValueError(f"{name} must be a positive integer <=65535")
        for name in ("timeout_s", "restart_delay_s", "fps"):
            value = getattr(self, name)
            if (value is None and name != "fps") or (value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))
                                      or not math.isfinite(value) or value <= 0)):
                raise ValueError(f"{name} must be finite and positive")
        if self.fps is not None:
            rate = Fraction(str(self.fps)).limit_denominator(1000000)
            if not 0 < rate.numerator <= 0xffffffff or rate.denominator > 0xffffffff:
                raise ValueError("fps cannot be represented by V4L2 uint32 rational fields")
        if self.fourcc is not None and (not isinstance(self.fourcc, str) or self.fourcc not in SUPPORTED_FORMATS):
            raise ValueError(f"unsupported single-planar fourcc: {self.fourcc}")
        if type(self.buffer_count) is not int or not 2 <= self.buffer_count <= 32:
            raise ValueError("buffer_count must be between 2 and 32")
        if type(self.restart_attempts) is not int or not 0 <= self.restart_attempts <= 100:
            raise ValueError("restart_attempts must be between 0 and 100")


class V4L2Capture:
    """Own the fd and MMAP buffers; return converted pixels before QBUF reuse."""
    def __init__(self, config):
        self.config = config
        self.fd = None
        self.maps = []
        self.streaming = False
        self.stopped = threading.Event()
        self.lock = threading.RLock()
        self.reported = {}

    def open(self):
        with self.lock:
            if self.stopped.is_set():
                raise InterruptedError("camera source closed")
            self._open()

    def _open(self):
        if platform.system() != "Linux" or C.sizeof(C.c_void_p) != 8 or platform.machine() not in {"x86_64", "aarch64", "arm64"}:
            raise RuntimeError("direct V4L2 ABI currently supports Linux LP64 x86_64/aarch64 only")
        try:
            self.fd = os.open(self.config.device, os.O_RDWR | os.O_NONBLOCK | os.O_CLOEXEC)
            cap = _ioctl(self.fd, 0, Capability(), 2)
            caps = cap.device_caps if cap.capabilities & 0x80000000 else cap.capabilities
            if not caps & 1 or not caps & 0x04000000:
                raise ValueError("camera requires single-planar VIDEO_CAPTURE and STREAMING; multiplanar-only nodes are unsupported")
            fmt = Format(type=1)
            _ioctl(self.fd, 4, fmt)
            if self.config.width is not None:
                fmt.fmt.pix.width, fmt.fmt.pix.height = self.config.width, self.config.height
            if self.config.fourcc is not None:
                fmt.fmt.pix.pixelformat = _fourcc(self.config.fourcc)
            fmt.fmt.pix.field = 1  # require progressive frames
            _ioctl(self.fd, 5, fmt)
            pix = fmt.fmt.pix
            code = int(pix.pixelformat).to_bytes(4, "little").decode("ascii", errors="replace")
            if code not in SUPPORTED_FORMATS or pix.field != 1:
                raise ValueError(f"unsupported negotiated format/field: {code}/{pix.field}")
            self.reported = {"width": int(pix.width), "height": int(pix.height), "fourcc": code,
                             "stride": int(pix.bytesperline), "sizeimage": int(pix.sizeimage),
                             "colorspace": int(pix.colorspace), "ycbcr_enc": int(pix.ycbcr_enc),
                             "quantization": int(pix.quantization), "driver": bytes(cap.driver).decode(errors="replace")}
            if code in {"NV12", "NV21", "YUYV", "UYVY"}:
                encoding = pix.ycbcr_enc or (2 if pix.colorspace in {3, 12} else (6 if pix.colorspace == 10 else (8 if pix.colorspace == 2 else 1)))
                quantization = pix.quantization or (1 if pix.colorspace == 7 else 2)
                if not pix.colorspace or encoding != 1 or quantization != 2:
                    raise ValueError("YUV conversion requires driver-declared BT.601 limited-range colorimetry")
            parm = StreamParameters(type=1)
            if self.config.fps is not None:
                rate = Fraction(str(self.config.fps)).limit_denominator(1000000)
                parm.parm.capture.timeperframe = FractionU32(rate.denominator, rate.numerator)
                _ioctl(self.fd, 22, parm)
            else:
                try:
                    _ioctl(self.fd, 21, parm)
                except OSError as exc:
                    if exc.errno not in (errno.EINVAL, errno.ENOTTY):
                        raise
            interval = parm.parm.capture.timeperframe
            self.reported["fps"] = interval.denominator / interval.numerator if interval.numerator else None
            request = _ioctl(self.fd, 8, RequestBuffers(count=self.config.buffer_count, type=1, memory=1))
            if not 2 <= request.count <= self.config.buffer_count:
                raise ValueError("driver buffer count is outside requested bounded allocation")
            self.reported["buffer_count"] = int(request.count)
            for index in range(request.count):
                buf = _ioctl(self.fd, 9, Buffer(index=index, type=1, memory=1))
                mapping = mmap.mmap(self.fd, buf.length, flags=mmap.MAP_SHARED, prot=mmap.PROT_READ | mmap.PROT_WRITE, offset=buf.m.offset)
                self.maps.append(mapping)
                _ioctl(self.fd, 15, Buffer(index=index, type=1, memory=1))
            _ioctl(self.fd, 18, C.c_int(1), 1)
            self.streaming = True
        except BaseException:
            self.close()
            raise

    def read(self):
        with self.lock:
            deadline = monotonic() + self.config.timeout_s
            while not self.stopped.is_set():
                left = deadline - monotonic()
                if left <= 0:
                    raise TimeoutError("V4L2 frame timeout")
                ready, _, _ = select.select([self.fd], [], [], min(left, 0.1))
                if not ready:
                    continue
                try:
                    buf = _ioctl(self.fd, 17, Buffer(type=1, memory=1))
                except OSError as exc:
                    if exc.errno == errno.EAGAIN:
                        continue
                    raise
                received = monotonic_ns()
                if buf.index >= len(self.maps):
                    raise RuntimeError("driver returned invalid buffer index")
                try:
                    if buf.flags & 0x40:
                        raise OSError(errno.EIO, "V4L2 buffer carries ERROR flag")
                    if not 0 < buf.bytesused <= len(self.maps[buf.index]):
                        raise ValueError("invalid V4L2 bytesused")
                    if buf.timestamp.tv_sec < 0 or not 0 <= buf.timestamp.tv_usec < 1000000:
                        raise ValueError("invalid V4L2 timeval")
                    view = memoryview(self.maps[buf.index])[:buf.bytesused]
                    try:
                        image = decode_buffer(view, self.reported["width"], self.reported["height"],
                                              self.reported["fourcc"], self.reported["stride"])
                    finally:
                        view.release()
                    stamp = buf.timestamp.tv_sec * 1000000000 + buf.timestamp.tv_usec * 1000
                    domain = "v4l2_monotonic" if buf.flags & 0xe000 == 0x2000 else "v4l2_unknown"
                    boundary = buf.flags & 0x70000
                    semantic = {0: "driver_end_of_frame",
                                0x10000: "driver_reported_start_of_exposure_unverified"}.get(
                                    boundary, "driver_timestamp_unknown_boundary")
                    return image, int(buf.sequence), stamp, domain, semantic, received
                finally:
                    _ioctl(self.fd, 15, Buffer(index=buf.index, type=1, memory=1))
            raise InterruptedError("camera source closed")

    def close(self):
        self.stopped.set()
        with self.lock:
            if self.fd is not None and self.streaming:
                try:
                    _ioctl(self.fd, 19, C.c_int(1), 1)
                except OSError:
                    pass  # disconnect must still release all mappings and fd
            self.streaming = False
            failure = None
            for mapping in self.maps:
                try:
                    mapping.close()
                except (OSError, BufferError) as exc:
                    failure = failure or exc
            self.maps.clear()
            if self.fd is not None:
                descriptor, self.fd = self.fd, None
                try:
                    os.close(descriptor)
                except OSError as exc:
                    failure = failure or exc
            if failure is not None:
                raise failure


class CameraSource:
    """Single-use source with a total bounded restart budget and kernel counters.

    A restart establishes a new stream_id; kernel sequence/timestamp values may
    reset. Application sequence stays increasing. close() interrupts select/retry.
    """
    def __init__(self, config: CameraConfig):
        self.config = config
        self.reported = {}
        self.stats = {"restarts": 0, "source_drops": 0, "errors": 0, "frames": 0}
        self.started = False
        self.closed = False
        self._stop = threading.Event()
        self._capture = None

    def __iter__(self):
        from .pipeline import Frame
        if self.started or self.closed:
            raise RuntimeError("source is closed or already consumed")
        self.started = True
        stream_id = 0
        previous = None
        try:
            while not self._stop.is_set():
                try:
                    if self._capture is None:
                        self._capture = V4L2Capture(self.config)
                        if self._stop.is_set():
                            return
                        self._capture.open()
                        self.reported = dict(self._capture.reported)
                    image, sequence, stamp, domain, semantic, received = self._capture.read()
                    if previous is not None:
                        delta = (sequence - previous) & 0xffffffff
                        if delta == 0 or delta >= 0x80000000:
                            raise ValueError("non-increasing V4L2 kernel frame sequence")
                        self.stats["source_drops"] += delta - 1
                    previous = sequence
                    frame = Frame(self.stats["frames"], image, stamp, domain, semantic, received,
                                  stream_id=stream_id, source_sequence=sequence)
                    self.stats["frames"] += 1
                    yield frame
                except (OSError, TimeoutError) as exc:
                    if self._stop.is_set():
                        return
                    self.stats["errors"] += 1
                    self._capture.close()
                    self._capture = None
                    if self.stats["restarts"] >= self.config.restart_attempts:
                        raise RuntimeError(f"camera disconnected or timed out; restart budget exhausted: {exc}") from exc
                    self.stats["restarts"] += 1
                    stream_id += 1
                    previous = None
                    if self._stop.wait(self.config.restart_delay_s):
                        return
        finally:
            self.close()

    def close(self):
        self._stop.set()
        self.closed = True
        if self._capture is not None:
            self._capture.close()
