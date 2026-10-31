"""Bounded original-payload recording and integrity-checked, metadata-rich replay.

The writer runs in acquisition before QBUF. Completion means writes returned,
not fsync/disk durability. A crash leaves an incomplete manifest; replaying its
verified complete prefix requires an explicit opt-in. Payload bytes are never
transcoded, and only one payload/JSON record is held in memory at a time.
"""
import hashlib
import json
from pathlib import Path
from threading import Event
from time import monotonic_ns

from .formats import SUPPORTED_FORMATS, decode_buffer, validate_colorimetry

SCHEMA_VERSION = 1
RECORDING_KIND = "rm27_original_payload_recording"
MAX_INDEX_LINE_BYTES = 1024 * 1024
DEFAULT_MAX_FRAME_BYTES = 128 * 1024 * 1024


class RecordingLimit(Exception):
    """The next whole frame would exceed the configured payload-byte limit."""


class RecordingWriteError(RuntimeError):
    """Storage failure; acquisition must stop rather than restart the camera."""


def _reject_nonfinite(value):
    raise ValueError(f"nonfinite JSON value in recording: {value}")


def _integer(value, name, minimum=0):
    if type(value) is not int or value < minimum:
        raise ValueError(f"recording {name} must be an integer >= {minimum}")
    return value


class _Ordering:
    """Constant-memory validation of the original acquisition timeline."""
    def __init__(self):
        self.previous = None
        self.source_gaps = self.application_gaps = self.streams = self.empty_stream_epochs = 0

    def observe(self, row, bytesused=None):
        if not isinstance(row, dict):
            raise ValueError("recording frame record must be an object")
        for name in ("sequence", "source_sequence", "stream_id", "timestamp_ns", "received_monotonic_ns", "driver_flags"):
            _integer(row.get(name), name)
        if row["driver_flags"] > 0xffffffff:
            raise ValueError("recording driver_flags exceeds uint32")
        if row["source_sequence"] > 0xffffffff:
            raise ValueError("recording source_sequence exceeds uint32")
        for name in ("clock_domain", "timestamp_semantic"):
            if not isinstance(row.get(name), str) or not row[name]:
                raise ValueError(f"recording {name} is required")
        fmt = row.get("format")
        if not isinstance(fmt, dict):
            raise ValueError("recording format must be an object")
        for name in ("width", "height"):
            _integer(fmt.get(name), name, 1)
            if fmt[name] > 65535:
                raise ValueError("recording dimensions exceed supported V4L2 configuration")
        _integer(fmt.get("stride"), "stride")
        sizeimage = _integer(fmt.get("sizeimage"), "sizeimage", 1)
        if sizeimage > 0xffffffff or fmt["stride"] > 0xffffffff:
            raise ValueError("recording negotiated layout exceeds V4L2 uint32")
        used = row.get("bytesused") if bytesused is None else bytesused
        if used is not None and _integer(used, "bytesused", 1) > sizeimage:
            raise ValueError("recording bytesused exceeds negotiated sizeimage")
        plane = row.get('plane_metadata')
        if 'plane_metadata' in row or fmt.get('capture_type') == 'mplane':
            if not isinstance(plane, dict):
                raise ValueError('recording plane_metadata must be an object for the declared memory-plane layout')
            offset = _integer(plane.get('data_offset'), 'plane data_offset')
            length = _integer(plane.get('length'), 'plane length', 1)
            plane_used = _integer(plane.get('bytesused'), 'plane bytesused', 1)
            if (used is None or plane_used != used or not offset < used <= length or
                    fmt.get('num_planes') != 1 or fmt.get('capture_type') != 'mplane'):
                raise ValueError('invalid recording single memory-plane bounds or provenance')
        if fmt.get("fourcc") not in SUPPORTED_FORMATS:
            raise ValueError("recording contains unsupported FOURCC")
        validate_colorimetry(fmt)
        previous = self.previous
        if previous is None:
            self.streams = 1
            self.empty_stream_epochs = row["stream_id"]
        else:
            if row["sequence"] <= previous["sequence"]:
                raise ValueError("duplicate or out-of-order recording application sequence")
            if row["received_monotonic_ns"] < previous["received_monotonic_ns"]:
                raise ValueError("recording original host receipt regressed")
            epoch_delta = row["stream_id"] - previous["stream_id"]
            if epoch_delta < 0:
                raise ValueError("invalid recording stream transition")
            if epoch_delta:
                self.streams += 1
                self.empty_stream_epochs += epoch_delta - 1
            else:
                for name in ("clock_domain", "timestamp_semantic", "format"):
                    if row[name] != previous[name]:
                        raise ValueError(f"recording {name} changed within stream")
                if row["timestamp_ns"] <= previous["timestamp_ns"]:
                    raise ValueError("recording source timestamp did not increase")
                delta = (row["source_sequence"] - previous["source_sequence"]) & 0xffffffff
                if not 0 < delta < 0x80000000:
                    raise ValueError("duplicate or out-of-order recording source sequence")
                self.source_gaps += delta - 1
            self.application_gaps += row["sequence"] - previous["sequence"] - 1
        # Keep a private order snapshot: callers may annotate the returned row.
        self.previous = {name: row[name] for name in ("sequence", "source_sequence", "stream_id",
                         "timestamp_ns", "received_monotonic_ns", "clock_domain", "timestamp_semantic")}
        self.previous["format"] = json.loads(json.dumps(row["format"], allow_nan=False))


def _write_all(stream, data):
    """FileIO can return short writes; never publish an index for partial data."""
    view = memoryview(data).cast('B')
    try:
        offset = 0
        while offset < len(view):
            written = stream.write(view[offset:])
            if written is None or written <= 0:
                raise OSError("recording write made no progress")
            offset += written
    finally:
        view.release()


class RecordingWriter:
    """Exclusive directory owner; synchronous append-only payload and index.

    max_bytes bounds payload.bin, not JSON metadata. At a limit the incomplete
    next frame is not written. On I/O failure partial evidence remains untouched.
    The manifest always starts incomplete and is finalized only after both files
    close successfully. No writer thread or additional frame queue is created.
    """
    def __init__(self, path, max_bytes):
        _integer(max_bytes, "max_bytes", 1)
        self.path = Path(path).resolve()
        self.path.mkdir(parents=True, exist_ok=False)
        self.payload = self.index = None
        self.closed = False
        self.failed = False
        self.ordering = _Ordering()
        # Import lazily: the shared histogram imports Frame/CameraSource itself.
        from .runtime import Latencies
        self.write_latency = Latencies()
        self._stats = {"frame_count": 0, "payload_bytes": 0, "write_total_ns": 0, "write_max_ns": 0}
        self.manifest = {"kind": RECORDING_KIND, "schema_version": SCHEMA_VERSION,
                         "complete": False, "reason": "recording_in_progress",
                         "max_payload_bytes": max_bytes, "payload_file": "payload.bin", "index_file": "frames.jsonl",
                         "completion_semantic": "application writes completed; disk durability is not guaranteed",
                         "created_monotonic_ns": monotonic_ns(), **self._stats}
        try:
            with (self.path / "manifest.json").open("x") as stream:
                stream.write(json.dumps(self.manifest, indent=2, allow_nan=False) + "\n")
            self.payload = (self.path / "payload.bin").open("xb", buffering=0)
            self.index = (self.path / "frames.jsonl").open("xb", buffering=0)
        except BaseException:
            for stream in (self.payload, self.index):
                if stream is not None:
                    stream.close()
            raise

    @property
    def stats(self):
        return {**self._stats, "write_overhead": self.write_latency.summary(), "complete": bool(self.manifest["complete"]),
                "reason": self.manifest["reason"],
                "write_mean_ns": self._stats["write_total_ns"] / self._stats["frame_count"] if self._stats["frame_count"] else None}

    def append(self, payload, metadata):
        if self.closed or self.failed:
            raise RecordingWriteError("recording writer is closed or failed")
        view = memoryview(payload)
        try:
            if not view.c_contiguous:
                raise ValueError('recording payload must be a contiguous byte buffer')
            size = view.nbytes
        finally:
            view.release()
        _integer(size, "bytesused", 1)
        if self._stats["payload_bytes"] + size > self.manifest["max_payload_bytes"]:
            raise RecordingLimit("recording payload byte limit reached before next whole frame")
        started = monotonic_ns()
        # Round-trip validates finite JSON and snapshots mutable per-stream data.
        row = json.loads(json.dumps(metadata, allow_nan=False))
        self.ordering.observe(row, bytesused=size)
        row.update(payload_offset=self._stats["payload_bytes"], bytesused=size,
                   sha256=hashlib.sha256(payload).hexdigest())
        try:
            payload_started = monotonic_ns()
            _write_all(self.payload, payload)
            # Full append overhead (including JSON/hash) is separately histogrammed.
            row["payload_write_ns"] = monotonic_ns() - payload_started
            encoded = (json.dumps(row, allow_nan=False, separators=(",", ":")) + "\n").encode("utf-8")
            if len(encoded) > MAX_INDEX_LINE_BYTES:
                raise ValueError("recording frame metadata exceeds bounded index record size")
            _write_all(self.index, encoded)
        except (OSError, ValueError) as exc:
            self.failed = True
            self.finish(complete=False, reason=f"write_failed: {exc}")
            raise RecordingWriteError(f"recording storage failure: {exc}") from exc
        elapsed = monotonic_ns() - started
        self.write_latency.add(elapsed)
        self._stats["frame_count"] += 1
        self._stats["payload_bytes"] += size
        self._stats["write_total_ns"] += elapsed
        self._stats["write_max_ns"] = max(self._stats["write_max_ns"], elapsed)
        return row

    def finish(self, complete=True, reason="completed"):
        if self.closed:
            return
        self.closed = True
        failure = None
        for stream in (self.payload, self.index):
            if stream is not None:
                try:
                    stream.close()
                except OSError as exc:
                    failure = failure or exc
        self.manifest.update(self._stats, write_overhead=self.write_latency.summary(), complete=bool(complete and not self.failed and failure is None),
                             reason=reason if failure is None else f"close_failed: {failure}",
                             finalized_monotonic_ns=monotonic_ns())
        try:
            # Atomic replacement prevents a torn final manifest from destroying
            # the initial incomplete manifest. No fsync/durability claim is made.
            temporary = self.path / ".manifest.tmp"
            with temporary.open("x") as stream:
                stream.write(json.dumps(self.manifest, indent=2, allow_nan=False) + "\n")
            temporary.replace(self.path / "manifest.json")
        except OSError as exc:
            failure = failure or exc
        if failure is not None:
            self.manifest.update(complete=False, reason=f"finalization_failed: {failure}")
        if failure is not None and not self.failed:
            self.failed = True
            raise RecordingWriteError(f"recording finalization failed: {failure}") from failure


class RecordingSource:
    """Integrity-checked raw replay; preserve acquisition and refresh replay clock."""
    def __init__(self, path, allow_incomplete=False, max_frame_bytes=DEFAULT_MAX_FRAME_BYTES):
        if type(allow_incomplete) is not bool:
            raise ValueError("allow_incomplete must be an explicit boolean")
        _integer(max_frame_bytes, "max_frame_bytes", 1)
        self.max_frame_bytes = max_frame_bytes
        requested = Path(path).resolve()
        self.path = requested if requested.is_dir() else requested.parent
        manifest_path = self.path / "manifest.json"
        if requested.is_file() and requested != manifest_path:
            raise ValueError("recording input must be its directory or manifest.json")
        manifest_size = manifest_path.stat().st_size
        if manifest_size > MAX_INDEX_LINE_BYTES:
            raise ValueError("recording manifest exceeds bounded metadata size")
        with manifest_path.open("rb") as stream:
            encoded = stream.read(manifest_size + 1)
        if len(encoded) != manifest_size:
            raise ValueError("recording manifest changed during replay")
        self.manifest = json.loads(encoded, parse_constant=_reject_nonfinite)
        if not isinstance(self.manifest, dict) or self.manifest.get("kind") != RECORDING_KIND or (type(self.manifest.get("schema_version")) is not int or self.manifest["schema_version"] != SCHEMA_VERSION):
            raise ValueError("unsupported recording manifest")
        if type(self.manifest.get("complete")) is not bool:
            raise ValueError("recording manifest completeness is invalid")
        self.allow_incomplete = allow_incomplete
        if not self.manifest["complete"] and not allow_incomplete:
            raise ValueError("recording is incomplete; use explicit allow_incomplete to replay its verified complete prefix")
        if self.manifest.get("payload_file") != "payload.bin" or self.manifest.get("index_file") != "frames.jsonl":
            raise ValueError("recording manifest file names are invalid")
        _integer(self.manifest.get("max_payload_bytes"), "max_payload_bytes", 1)
        for name in ("frame_count", "payload_bytes"):
            _integer(self.manifest.get(name), name)
        self.files = [manifest_path, self.path / "frames.jsonl", self.path / "payload.bin"]
        if not all(file.is_file() for file in self.files):
            raise ValueError("recording files are missing")
        self.closed = False
        self.started = False
        self.stopped = Event()
        self.ordering = _Ordering()
        self.stats = {"frames": 0, "source_drops": 0, "application_sequence_gaps": 0,
                      "streams": 0, "empty_stream_epochs": 0, "verified_payload_bytes": 0, "incomplete_tail": False}
        self.reported = {"recording_manifest": str(manifest_path), "recording_complete": self.manifest["complete"]}

    def __iter__(self):
        from .pipeline import Frame
        if self.started or self.closed:
            raise RuntimeError("recording source is closed or already consumed")
        self.started = True
        offset = 0
        count = 0
        payload_size = self.files[2].stat().st_size
        try:
            with self.files[1].open("rb") as index, self.files[2].open("rb") as payload:
                while not self.stopped.is_set():
                    line = index.readline(MAX_INDEX_LINE_BYTES + 1)
                    if not line:
                        break
                    if len(line) > MAX_INDEX_LINE_BYTES:
                        raise ValueError("recording index record exceeds bounded size")
                    if not line.endswith(b"\n"):
                        if self.allow_incomplete and not self.manifest["complete"]:
                            self.stats["incomplete_tail"] = True
                            break
                        raise ValueError("truncated recording index")
                    try:
                        row = json.loads(line, parse_constant=_reject_nonfinite)
                    except (ValueError, UnicodeError) as exc:
                        raise ValueError("corrupt recording index JSON") from exc
                    self.ordering.observe(row)
                    fmt = row["format"]
                    size = _integer(row.get("bytesused"), "bytesused", 1)
                    if size > self.max_frame_bytes or fmt["width"] * fmt["height"] * 3 > self.max_frame_bytes:
                        raise ValueError("recording frame exceeds replay max_frame_bytes memory limit")
                    start = _integer(row.get("payload_offset"), "payload_offset")
                    if start != offset:
                        raise ValueError("recording payload offsets are not contiguous")
                    end = start + size
                    if end > self.manifest["max_payload_bytes"]:
                        raise ValueError("recording payload exceeds declared byte limit")
                    if end > payload_size:
                        if self.allow_incomplete and not self.manifest["complete"]:
                            if index.read(1):
                                raise ValueError("truncated recording payload precedes further index records")
                            self.stats["incomplete_tail"] = True
                            break
                        raise ValueError("truncated recording payload")
                    data = payload.read(size)
                    if len(data) != size:
                        raise ValueError("recording payload changed during replay")
                    if not isinstance(row.get("sha256"), str) or hashlib.sha256(data).hexdigest() != row["sha256"]:
                        raise ValueError("recording payload hash mismatch")
                    active = memoryview(data)[row.get('plane_metadata', {}).get('data_offset', 0):]
                    try:
                        image = decode_buffer(active, fmt["width"], fmt["height"], fmt["fourcc"], fmt["stride"])
                    finally:
                        active.release()
                    data = None  # release raw bytes before a queued consumer retains pixels
                    offset = end
                    count += 1
                    self.stats.update(frames=count, source_drops=self.ordering.source_gaps,
                                      application_sequence_gaps=self.ordering.application_gaps,
                                      streams=self.ordering.streams, empty_stream_epochs=self.ordering.empty_stream_epochs,
                                      verified_payload_bytes=offset)
                    yield Frame(row["sequence"], image, row["timestamp_ns"], row["clock_domain"],
                                row["timestamp_semantic"], row["received_monotonic_ns"],
                                stream_id=row["stream_id"], source_sequence=row["source_sequence"],
                                original_received_monotonic_ns=row["received_monotonic_ns"],
                                replay_received_monotonic_ns=monotonic_ns(), acquisition_metadata=row)
                if not self.stopped.is_set():
                    if self.manifest["complete"]:
                        if (count, offset) != (self.manifest["frame_count"], self.manifest["payload_bytes"]) or offset != payload_size:
                            raise ValueError("complete recording counts or payload size disagree with manifest")
                    elif offset != payload_size:
                        self.stats["incomplete_tail"] = True
        finally:
            self.close()

    def close(self):
        self.closed = True
        self.stopped.set()
