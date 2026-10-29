"""Owned BGR frames, explicit clock semantics, and small OpenCV adapters."""
from dataclasses import dataclass
import math
from pathlib import Path
from time import monotonic_ns
from threading import RLock
from typing import Iterator, Protocol

import cv2
import numpy as np


@dataclass(frozen=True)
class Frame:
    sequence: int
    image: np.ndarray  # owned uint8 BGR array; caller may retain it
    timestamp_ns: int
    clock_domain: str
    timestamp_semantic: str
    received_monotonic_ns: int
    stream_id: int = 0
    source_sequence: int | None = None


class FrameSource(Protocol):
    def __iter__(self) -> Iterator[Frame]: ...
    def close(self) -> None: ...


def positive(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be finite and positive")


@dataclass(frozen=True)
class ProcessingConfig:
    fps: float = 30.0
    width: int | None = None
    height: int | None = None
    grayscale: bool = False
    max_frames: int | None = None

    def __post_init__(self):
        positive(self.fps, "fps")
        if self.fps > 1e9:
            raise ValueError("fps exceeds nanosecond resolution")
        if (self.width is None) != (self.height is None):
            raise ValueError("width and height must be specified together")
        for name in ("width", "height", "max_frames"):
            value = getattr(self, name)
            if value is not None and (type(value) is not int or value <= 0):
                raise ValueError(f"{name} must be a positive integer")
        if type(self.grayscale) is not bool:
            raise ValueError("grayscale must be boolean")


def preprocess(image: np.ndarray, config: ProcessingConfig) -> np.ndarray:
    if image.dtype != np.uint8 or image.ndim != 3 or image.shape[2] != 3 or not image.size:
        raise ValueError("expected nonempty uint8 BGR image")
    result = image
    if config.width is not None:
        result = cv2.resize(result, (config.width, config.height), interpolation=cv2.INTER_AREA)
    if config.grayscale:
        result = cv2.cvtColor(result, cv2.COLOR_BGR2GRAY)
    # OpenCV transformations already own their output; copy only the identity path.
    return result.copy() if result is image else result


class ReplaySource:
    """Single image, sorted image directory, or file video; never opens a device.

    The timeline is always sequence/fps, including video. This deliberately
    avoids interpreting backend-dependent POS_MSEC as capture/exposure timing.
    """
    EXTENSIONS = {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff"}

    def __init__(self, path: Path | str, fps: float):
        positive(fps, "fps")
        if fps > 1e9:
            raise ValueError("fps exceeds nanosecond resolution")
        self.path = Path(path).resolve()
        self.fps = fps
        self.capture = None
        self._io_lock = RLock()
        self.closed = False
        self.started = False
        if self.path.is_dir():
            self.files = sorted(p for p in self.path.iterdir() if p.suffix.lower() in self.EXTENSIONS and p.is_file())
            if not self.files:
                raise ValueError("image directory is empty")
        elif self.path.is_file():
            self.files = [self.path] if self.path.suffix.lower() in self.EXTENSIONS else None
        else:
            raise ValueError(f"input is not a regular file or image directory: {self.path}")

    def __iter__(self):
        if self.closed or self.started:
            raise RuntimeError("source is closed or already consumed")
        self.started = True
        try:
            if self.files is None:
                with self._io_lock:
                    if self.closed:
                        return
                    self.capture = cv2.VideoCapture(str(self.path))
                    if not self.capture.isOpened():
                        raise ValueError(f"cannot open video: {self.path}")
            sequence = 0
            while not self.closed:
                if self.files is not None:
                    if sequence == len(self.files):
                        break
                    image = cv2.imread(str(self.files[sequence]), cv2.IMREAD_COLOR)
                    if image is None:
                        raise ValueError(f"cannot decode image: {self.files[sequence]}")
                else:
                    with self._io_lock:
                        if self.closed:
                            break
                        ok, image = self.capture.read()
                    if not ok:
                        if sequence == 0:
                            raise ValueError("video contains no decodable frames")
                        break
                yield Frame(sequence, image, round(sequence * 1e9 / self.fps),
                            "replay", "synthetic_sequence_fps", monotonic_ns())
                sequence += 1
        finally:
            self.close()

    def close(self):
        self.closed = True
        # Never release a decoder concurrently with read(); generator finally
        # releases it after an in-flight read returns.
        if self._io_lock.acquire(blocking=False):
            try:
                if self.capture is not None:
                    self.capture.release()
            finally:
                self._io_lock.release()


class TimestampValidator:
    """Validate one stream epoch at a time without inventing clock conversions."""
    def __init__(self):
        self.previous = None
        self.streams = 0
        self.sequence_gaps = 0

    def observe(self, frame: Frame):
        for name in ("sequence", "timestamp_ns", "received_monotonic_ns", "stream_id"):
            if type(getattr(frame, name)) is not int or getattr(frame, name) < 0:
                raise ValueError(f"frame {name} must be a nonnegative integer")
        if frame.source_sequence is not None and (type(frame.source_sequence) is not int or frame.source_sequence < 0):
            raise ValueError("invalid source_sequence")
        if not frame.clock_domain or not frame.timestamp_semantic:
            raise ValueError("frame clock provenance is required")
        previous = self.previous
        if previous is not None:
            if frame.sequence <= previous.sequence:
                raise ValueError("application sequence is not strictly increasing")
            if frame.received_monotonic_ns < previous.received_monotonic_ns:
                raise ValueError("host receipt clock regressed")
            if frame.stream_id < previous.stream_id:
                raise ValueError("stream epoch regressed")
            if frame.stream_id == previous.stream_id:
                if (frame.clock_domain, frame.timestamp_semantic) != (previous.clock_domain, previous.timestamp_semantic):
                    raise ValueError("clock provenance changed within stream")
                if frame.timestamp_ns <= previous.timestamp_ns:
                    raise ValueError("source timestamp is not strictly increasing")
            self.sequence_gaps += frame.sequence - previous.sequence - 1
        if previous is None or previous.stream_id != frame.stream_id:
            self.streams += 1
        self.previous = frame


# Preserve S1 public imports; the old OpenCV camera adapter is retired.
from .camera import CameraConfig, CameraSource  # noqa: E402
