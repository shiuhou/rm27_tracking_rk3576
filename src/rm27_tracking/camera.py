"""Linux V4L2 MMAP acquisition for single memory-plane linear formats.

Both VIDEO_CAPTURE and VIDEO_CAPTURE_MPLANE APIs are supported. Multiplanar
formats requiring separate memory planes (e.g. NM12/NM21) remain unsupported.

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
import sys
import threading
from time import monotonic, monotonic_ns

from .formats import SUPPORTED_FORMATS, decode_buffer, validate_colorimetry

U32 = C.c_uint32
VIDEO_CAPTURE = 1
VIDEO_CAPTURE_MPLANE = 9
VIDEO_MAX_PLANES = 8


class Capability(C.Structure):
    _fields_ = [("driver", C.c_char * 16), ("card", C.c_char * 32), ("bus_info", C.c_char * 32),
                ("version", U32), ("capabilities", U32), ("device_caps", U32), ("reserved", U32 * 3)]


class PixelFormat(C.Structure):
    _fields_ = [(name, U32) for name in ("width", "height", "pixelformat", "field", "bytesperline",
               "sizeimage", "colorspace", "priv", "flags", "ycbcr_enc", "quantization", "xfer_func")]


class PlanePixelFormat(C.Structure):
    _pack_ = 1
    _fields_ = [("sizeimage", U32), ("bytesperline", U32), ("reserved", C.c_uint16 * 6)]


class PixelFormatMplane(C.Structure):
    _pack_ = 1
    _fields_ = [("width", U32), ("height", U32), ("pixelformat", U32), ("field", U32),
                ("colorspace", U32), ("plane_fmt", PlanePixelFormat * VIDEO_MAX_PLANES),
                ("num_planes", C.c_uint8), ("flags", C.c_uint8), ("ycbcr_enc", C.c_uint8),
                ("quantization", C.c_uint8), ("xfer_func", C.c_uint8), ("reserved", C.c_uint8 * 7)]


class FormatUnion(C.Union):
    _fields_ = [("pix", PixelFormat), ("pix_mp", PixelFormatMplane), ("raw", C.c_uint8 * 200), ("alignment", C.c_void_p)]


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


class PlaneMemory(C.Union):
    _fields_ = [("mem_offset", U32), ("userptr", C.c_ulong), ("fd", C.c_int32)]


class Plane(C.Structure):
    _fields_ = [("bytesused", U32), ("length", U32), ("m", PlaneMemory),
                ("data_offset", U32), ("reserved", U32 * 11)]


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


class QueryControl(C.Structure):
    _fields_ = [("id", U32), ("type", U32), ("name", C.c_char * 32),
                ("minimum", C.c_int32), ("maximum", C.c_int32), ("step", C.c_int32),
                ("default_value", C.c_int32), ("flags", U32), ("reserved", U32 * 2)]


class Control(C.Structure):
    _fields_ = [("id", U32), ("value", C.c_int32)]


def query_exposure_controls(fd):
    """Read standard controls once per open. Values are NOT frame synchronized."""
    controls = []
    for name, identity, units in (("exposure_auto", 0x009a0901, "V4L2 exposure mode enum"),
                                  ("exposure_absolute", 0x009a0902, "100 microseconds"),
                                  ("exposure_auto_priority", 0x009a0903, "boolean"),
                                  ("exposure", 0x00980911, "driver-defined; physical units unverified")):
        item = {"name": name, "id": identity, "units": units, "available": False,
                "read_start_monotonic_ns": monotonic_ns(),
                "provenance": "VIDIOC_QUERYCTRL/VIDIOC_G_CTRL readback; not frame synchronized"}
        try:
            query = _ioctl(fd, 36, QueryControl(id=identity))
            item.update(driver_name=bytes(query.name).decode(errors="replace"),
                        supported=True,
                        flags=int(query.flags), minimum=int(query.minimum), maximum=int(query.maximum),
                        step=int(query.step), default_value=int(query.default_value))
            if query.flags & 1:
                item['status'] = 'disabled'
            else:
                value = _ioctl(fd, 27, Control(id=identity))
                item.update(available=True, value=int(value.value), status='read')
        except OSError as exc:
            item.update(status='unsupported' if exc.errno in (errno.EINVAL, errno.ENOTTY) else 'read_failed',
                        errno=exc.errno)
        item['read_end_monotonic_ns'] = monotonic_ns()
        controls.append(item)
    return controls


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
    capture_type: str = "auto"

    def __post_init__(self):
        if not isinstance(self.device, str) or not self.device.startswith("/dev/video"):
            raise ValueError("device must be an explicit /dev/video path")
        if self.backend != "v4l2":
            raise ValueError("only direct v4l2 is implemented")
        if self.capture_type not in {"auto", "single", "mplane"}:
            raise ValueError("capture_type must be auto, single or mplane")
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
            raise ValueError(f"unsupported linear single memory-plane fourcc: {self.fourcc}")
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
        self.raw_observer = None
        self.last_metadata = None
        self.buffer_type = VIDEO_CAPTURE

    def open(self):
        with self.lock:
            if self.stopped.is_set():
                raise InterruptedError("camera source closed")
            self._open()

    def _buffer_ioctl(self, number, index=0):
        """Keep a plane array alive across each ioctl's nested userspace pointer."""
        buf = Buffer(index=index, type=self.buffer_type, memory=1)
        planes = None
        if self.buffer_type == VIDEO_CAPTURE_MPLANE:
            planes = (Plane * VIDEO_MAX_PLANES)()
            buf.m.planes = C.addressof(planes)
            buf.length = 1 if number == 15 else VIDEO_MAX_PLANES
            if number == 15:
                planes[0].length = len(self.maps[index])
        _ioctl(self.fd, number, buf)
        if planes is not None and buf.length != 1 and number != 17:
            raise ValueError("V4L2 capture requires exactly one memory plane")
        return buf, planes

    def _open(self):
        if platform.system() != "Linux" or C.sizeof(C.c_void_p) != 8 or platform.machine() not in {"x86_64", "aarch64", "arm64"}:
            raise RuntimeError("direct V4L2 ABI currently supports Linux LP64 x86_64/aarch64 only")
        try:
            self.fd = os.open(self.config.device, os.O_RDWR | os.O_NONBLOCK | os.O_CLOEXEC)
            cap = _ioctl(self.fd, 0, Capability(), 2)
            caps = cap.device_caps if cap.capabilities & 0x80000000 else cap.capabilities
            if not caps & 0x04000000:
                raise ValueError("camera requires STREAMING capability")
            requested = self.config.capture_type
            if requested == "auto":
                self.buffer_type = VIDEO_CAPTURE if caps & 1 else VIDEO_CAPTURE_MPLANE
            else:
                self.buffer_type = VIDEO_CAPTURE if requested == "single" else VIDEO_CAPTURE_MPLANE
            required_cap = 1 if self.buffer_type == VIDEO_CAPTURE else 0x1000
            if not caps & required_cap:
                raise ValueError(f"camera does not advertise requested capture_type: {requested}")
            fmt = Format(type=self.buffer_type)
            _ioctl(self.fd, 4, fmt)
            pix = fmt.fmt.pix if self.buffer_type == VIDEO_CAPTURE else fmt.fmt.pix_mp
            if self.config.width is not None:
                pix.width, pix.height = self.config.width, self.config.height
            if self.config.fourcc is not None:
                pix.pixelformat = _fourcc(self.config.fourcc)
            pix.field = 1  # require progressive frames
            _ioctl(self.fd, 5, fmt)
            if self.buffer_type == VIDEO_CAPTURE_MPLANE and pix.num_planes != 1:
                raise ValueError("V4L2 negotiated format requires exactly one memory plane; separate planes are unsupported")
            layout = pix if self.buffer_type == VIDEO_CAPTURE else pix.plane_fmt[0]
            code = int(pix.pixelformat).to_bytes(4, "little").decode("ascii", errors="replace")
            if code not in SUPPORTED_FORMATS or pix.field != 1:
                raise ValueError(f"unsupported negotiated format/field: {code}/{pix.field}")
            self.reported = {"width": int(pix.width), "height": int(pix.height), "fourcc": code,
                             "stride": int(layout.bytesperline), "sizeimage": int(layout.sizeimage),
                             "capture_type": "single" if self.buffer_type == VIDEO_CAPTURE else "mplane",
                             "buffer_type": self.buffer_type, "num_planes": 1,
                             "colorspace": int(pix.colorspace), "ycbcr_enc": int(pix.ycbcr_enc),
                             "quantization": int(pix.quantization), "xfer_func": int(pix.xfer_func),
                             "field": int(pix.field), "driver": bytes(cap.driver).decode(errors="replace")}
            validate_colorimetry(self.reported)
            parm = StreamParameters(type=self.buffer_type)
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
            self.reported['exposure_controls'] = query_exposure_controls(self.fd)
            request = _ioctl(self.fd, 8, RequestBuffers(count=self.config.buffer_count, type=self.buffer_type, memory=1))
            if not 2 <= request.count <= self.config.buffer_count:
                raise ValueError("driver buffer count is outside requested bounded allocation")
            self.reported["buffer_count"] = int(request.count)
            for index in range(request.count):
                buf, planes = self._buffer_ioctl(9, index)
                length = buf.length if planes is None else planes[0].length
                offset = buf.m.offset if planes is None else planes[0].m.mem_offset
                if length <= 0 or length < self.reported["sizeimage"]:
                    raise ValueError("V4L2 MMAP buffer is smaller than negotiated sizeimage")
                mapping = mmap.mmap(self.fd, length, flags=mmap.MAP_SHARED, prot=mmap.PROT_READ | mmap.PROT_WRITE, offset=offset)
                self.maps.append(mapping)
                self._buffer_ioctl(15, index)
            _ioctl(self.fd, 18, C.c_int(self.buffer_type), 1)
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
                    buf, planes = self._buffer_ioctl(17)
                except OSError as exc:
                    if exc.errno == errno.EAGAIN:
                        continue
                    raise
                received = monotonic_ns()
                if buf.index >= len(self.maps):
                    raise RuntimeError("driver returned invalid buffer index")
                try:
                    if planes is not None and buf.length != 1:
                        raise ValueError("V4L2 dequeued buffer requires exactly one memory plane")
                    if buf.flags & 0x40:
                        raise OSError(errno.EIO, "V4L2 buffer carries ERROR flag")
                    bytesused = int(buf.bytesused if planes is None else planes[0].bytesused)
                    data_offset = 0 if planes is None else int(planes[0].data_offset)
                    if not 0 <= data_offset < bytesused <= len(self.maps[buf.index]):
                        raise ValueError("invalid V4L2 bytesused/data_offset")
                    if planes is not None and planes[0].length != len(self.maps[buf.index]):
                        raise ValueError("V4L2 dequeued memory-plane length differs from mapping")
                    if buf.timestamp.tv_sec < 0 or not 0 <= buf.timestamp.tv_usec < 1000000:
                        raise ValueError("invalid V4L2 timeval")
                    stamp = buf.timestamp.tv_sec * 1000000000 + buf.timestamp.tv_usec * 1000
                    domain = "v4l2_monotonic" if buf.flags & 0xe000 == 0x2000 else "v4l2_unknown"
                    boundary = buf.flags & 0x70000
                    semantic = {0: "driver_end_of_frame",
                                0x10000: "driver_reported_start_of_exposure_unverified"}.get(
                                    boundary, "driver_timestamp_unknown_boundary")
                    self.last_metadata = {"source_sequence": int(buf.sequence), "timestamp_ns": stamp,
                                          "clock_domain": domain, "timestamp_semantic": semantic,
                                          "received_monotonic_ns": received, "driver_flags": int(buf.flags),
                                          "field": int(buf.field), "format": dict(self.reported)}
                    if planes is not None:
                        self.last_metadata["plane_metadata"] = {"data_offset": data_offset,
                                                                "length": int(planes[0].length),
                                                                "bytesused": bytesused}
                    view = memoryview(self.maps[buf.index])[:bytesused]
                    try:
                        if self.raw_observer is not None:
                            self.raw_observer(view, self.last_metadata)
                        active = view[data_offset:]
                        try:
                            image = decode_buffer(active, self.reported["width"], self.reported["height"],
                                                  self.reported["fourcc"], self.reported["stride"])
                        finally:
                            active.release()
                    finally:
                        view.release()
                    return image, int(buf.sequence), stamp, domain, semantic, received
                finally:
                    active_error = sys.exc_info()[0] is not None
                    try:
                        self._buffer_ioctl(15, buf.index)
                    except OSError:
                        if not active_error:
                            raise
                        # Preserve the primary decode/storage/limit exception;
                        # a disconnected QBUF must not turn it into camera retry.
            raise InterruptedError("camera source closed")

    def close(self):
        self.stopped.set()
        with self.lock:
            if self.fd is not None and self.streaming:
                try:
                    _ioctl(self.fd, 19, C.c_int(self.buffer_type), 1)
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
    def __init__(self, config: CameraConfig, recorder=None):
        self.config = config
        self.recorder = recorder
        self.reported = {}
        self.stats = {"restarts": 0, "source_drops": 0, "errors": 0, "frames": 0}
        self.started = False
        self.closed = False
        self._stop = threading.Event()
        self._capture = None

    def __iter__(self):
        from .pipeline import Frame
        from .recording import RecordingLimit
        if self.started or self.closed:
            raise RuntimeError("source is closed or already consumed")
        self.started = True
        stream_id = 0
        previous = None
        next_sequence = 0
        try:
            while not self._stop.is_set():
                try:
                    if self._capture is None:
                        self._capture = V4L2Capture(self.config)
                        def record_payload(payload, metadata):
                            nonlocal next_sequence
                            if previous is not None:
                                delta = (metadata['source_sequence'] - previous) & 0xffffffff
                                if delta == 0 or delta >= 0x80000000:
                                    raise ValueError('non-increasing V4L2 kernel frame sequence')
                            metadata.update(sequence=next_sequence, stream_id=stream_id)
                            if self.recorder is not None:
                                metadata.update(self.recorder.append(payload, metadata))
                            next_sequence += 1
                        self._capture.raw_observer = record_payload
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
                    acquired = getattr(self._capture, 'last_metadata', None)
                    app_sequence = acquired['sequence'] if acquired is not None else next_sequence
                    if acquired is None:
                        next_sequence += 1
                    frame = Frame(app_sequence, image, stamp, domain, semantic, received,
                                  stream_id=stream_id, source_sequence=sequence,
                                  original_received_monotonic_ns=received,
                                  acquisition_metadata=acquired)
                    self.stats["frames"] += 1
                    yield frame
                except RecordingLimit:
                    self.stats['stop_reason'] = 'recording_byte_limit'
                    return
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
        except GeneratorExit:
            raise
        except BaseException as exc:
            if self.recorder is not None:
                self.recorder.finish(complete=False, reason=str(exc))
            raise
        finally:
            self.close()

    def close(self):
        self._stop.set()
        self.closed = True
        if self._capture is not None:
            self._capture.close()
