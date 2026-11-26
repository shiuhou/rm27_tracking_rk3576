"""One bounded acquisition/processing path for replay and live sources."""
from collections import deque
from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import platform
import resource
from threading import Condition, Thread
from time import monotonic_ns, perf_counter_ns, process_time_ns
from typing import Callable

import cv2
import numpy as np

from . import __version__
from .pipeline import Frame, FrameSource, ProcessingConfig, TimestampValidator, positive, preprocess


@dataclass(frozen=True)
class RuntimeConfig:
    queue_capacity: int = 2
    queue_policy: str = "block"
    duration_s: float | None = None
    log_every: int = 1
    hash_frames: bool = True
    opencv_threads: int = 1

    def __post_init__(self):
        if type(self.queue_capacity) is not int or not 1 <= self.queue_capacity <= 64:
            raise ValueError("queue_capacity must be an integer in [1,64]")
        if self.queue_policy not in ("block", "latest"):
            raise ValueError("queue_policy must be block or latest")
        if self.duration_s is not None:
            positive(self.duration_s, "duration_s")
        if type(self.log_every) is not int or self.log_every < 0:
            raise ValueError("log_every must be a nonnegative integer (0 disables frame logs)")
        if type(self.hash_frames) is not bool:
            raise ValueError("hash_frames must be boolean")
        if type(self.opencv_threads) is not int or self.opencv_threads < 1:
            raise ValueError("opencv_threads must be a positive integer")


class Latencies:
    """Constant-memory log histogram: percentile upper edges (<=3% + 1 microsecond).

    Exact mean/max; 768 buckets of logarithm base 1.03 in microseconds.
    All samples contribute, including long sustained runs; no biased ring sample.
    """
    def __init__(self):
        self.histogram = np.zeros(768, dtype=np.int64)
        self.count = self.total = self.maximum = 0

    def add(self, ns):
        ns = max(0, int(ns))
        bucket = min(767, int(np.ceil(np.log1p(ns / 1000) / np.log(1.03))))
        self.histogram[bucket] += 1
        self.count += 1
        self.total += ns
        self.maximum = max(self.maximum, ns)

    def summary(self):
        if not self.count:
            return {"count": 0, "mean_ms": None, "p50_ms": None, "p95_ms": None,
                    "p99_ms": None, "max_ms": None}
        result = {"count": self.count, "mean_ms": self.total / self.count / 1e6,
                  "max_ms": self.maximum / 1e6}
        cumulative = np.cumsum(self.histogram)
        for name, fraction in (("p50_ms", .5), ("p95_ms", .95), ("p99_ms", .99)):
            bucket = int(np.searchsorted(cumulative, int(np.ceil(self.count * fraction))))
            result[name] = min(self.maximum / 1e6, (1.03 ** bucket - 1) / 1000)
        return result


@dataclass
class Packet:
    frame: Frame
    acquisition_ns: int
    queued_ns: int
    queue_depth: int = 0


class RunDeadline(Exception):
    """Internal finite-run deadline; distinct from a camera timeout."""


class BufferedSource:
    """Single producer, bounded queue. block preserves replay; latest drops oldest.

    Source validation happens BEFORE queue drops so an invalid frame cannot be
    hidden by backpressure. Source close must cancel an outstanding read within
    a bounded timeout. Worker is joined before reporting successful completion.
    """
    def __init__(self, source: FrameSource, capacity=2, policy="block", max_capture_frames=None):
        RuntimeConfig(queue_capacity=capacity, queue_policy=policy)
        self.source = source
        self.max_capture_frames = max_capture_frames
        self.capacity, self.policy = capacity, policy
        self.condition = Condition()
        self.queue = deque()
        self.closed = self.done = self.started = False
        self.error = None
        self.captured = self.dropped = self.high_water = self.discarded_on_stop = 0
        self.acquisition = Latencies()
        self.validation = TimestampValidator()
        self.first_received = self.last_received = None
        self.last_epoch = None
        self.discarded_on_epoch_change = 0
        self.thread = Thread(target=self._produce, name="rm27-acquisition", daemon=True)

    def _produce(self):
        iterator = None
        try:
            iterator = iter(self.source)
            while True:
                with self.condition:
                    if self.closed or (self.max_capture_frames is not None and self.captured >= self.max_capture_frames):
                        break
                    while self.policy == "block" and len(self.queue) >= self.capacity and not self.closed:
                        self.condition.wait()
                    if self.closed:
                        break
                before = perf_counter_ns()
                try:
                    frame = next(iterator)
                except StopIteration:
                    break
                elapsed = perf_counter_ns() - before
                self.validation.observe(frame)
                with self.condition:
                    self.acquisition.add(elapsed)
                    self.captured += 1
                    if self.first_received is None:
                        self.first_received = frame.host_received_monotonic_ns
                    self.last_received = frame.host_received_monotonic_ns
                    if self.closed:
                        self.discarded_on_stop += 1
                        break
                    if self.policy == "latest" and self.last_epoch != frame.stream_id:
                        self.discarded_on_epoch_change += len(self.queue)
                        self.queue.clear()
                    self.last_epoch = frame.stream_id
                    if len(self.queue) == self.capacity:
                        self.queue.popleft()
                        self.dropped += 1
                    self.queue.append(Packet(frame, elapsed, monotonic_ns()))
                    self.high_water = max(self.high_water, len(self.queue))
                    self.condition.notify_all()
        except BaseException as exc:
            with self.condition:
                self.error = exc
        finally:
            try:
                try:
                    if iterator is not None and hasattr(iterator, "close"):
                        iterator.close()
                finally:
                    self.source.close()
            except BaseException as exc:
                if self.error is None:
                    self.error = exc
            with self.condition:
                self.done = True
                self.condition.notify_all()

    def __iter__(self):
        if not self.started and not self.closed:
            self.started = True
            self.thread.start()
        return self

    def __next__(self):
        return self.get()

    def get(self, deadline_ns=None):
        with self.condition:
            while not self.queue and not self.done and not self.closed:
                if deadline_ns is not None and perf_counter_ns() >= deadline_ns:
                    raise RunDeadline()
                self.condition.wait(.1)
            if self.queue:
                packet = self.queue.popleft()
                packet.queue_depth = len(self.queue)
                self.condition.notify_all()
                return packet
            if self.error is not None:
                raise self.error
            raise StopIteration

    def close(self):
        with self.condition:
            self.closed = True
            self.condition.notify_all()
        failure = None
        try:
            self.source.close()
        except BaseException as exc:
            failure = exc
        finally:
            if self.started:
                # Source cancellation must bound read(); never claim clean
                # shutdown for a stuck decoder or kernel ioctl.
                self.thread.join(timeout=5)
                if self.thread.is_alive() and failure is None:
                    failure = RuntimeError("acquisition worker did not stop within 5 seconds")
            with self.condition:
                self.discarded_on_stop += len(self.queue)
                self.queue.clear()
        if failure is not None:
            raise failure

    def summary(self):
        with self.condition:
            span = 0 if self.first_received is None else self.last_received - self.first_received
            return {"captured": self.captured, "queue_drops": self.dropped,
                    "queue_capacity": self.capacity, "queue_high_water": self.high_water,
                    "discarded_on_stop": self.discarded_on_stop,
                    "discarded_on_epoch_change": self.discarded_on_epoch_change,
                    "streams": self.validation.streams,
                    "application_sequence_gaps": self.validation.sequence_gaps,
                    "acquisition_fps": (self.captured - 1) * 1e9 / span if span > 0 else None,
                    "acquisition": self.acquisition.summary()}


def rss_bytes():
    """Current resident bytes on Linux; absent on other platforms."""
    try:
        return int(Path('/proc/self/statm').read_text().split()[1]) * os.sysconf('SC_PAGE_SIZE')
    except (OSError, ValueError, IndexError):
        return None


def generic_statistics(image):
    """Small replaceable CPU processor: per-channel mean and standard deviation."""
    mean, deviation = cv2.meanStdDev(image)
    return {"mean": mean.ravel().tolist(), "stddev": deviation.ravel().tolist()}


def run_pipeline(source, output, processing: ProcessingConfig, runtime: RuntimeConfig,
                 processor: Callable = generic_statistics, metadata=None, *,
                 frame_processor=None, stop_requested=None, borrow_input=False):
    """Run shared acquisition. With borrow_input, callbacks must not mutate identity frames."""
    output = Path(output)
    # The caller creates a fresh run directory (and optional input manifest).
    output.mkdir(parents=True, exist_ok=True)
    for name in ("summary.json", "frames.jsonl", "publications.jsonl", "resources.jsonl", "preview.png"):
        if (output / name).exists():
            raise FileExistsError(f"refusing to overwrite run artifact: {output / name}")
    # Atomic reservation also prevents concurrent runs from owning one summary.
    with (output / "summary.json").open("x"):
        pass
    cv2.setNumThreads(runtime.opencv_threads)
    buffered = BufferedSource(source, runtime.queue_capacity, runtime.queue_policy,
                              max_capture_frames=processing.max_frames if runtime.queue_policy == "block" else None)
    stages = {name: Latencies() for name in ('queue_wait', 'validation', 'preprocessing', 'processing', 'preprocess_and_process', 'logging', 'host_residence', 'host_to_processing_complete')}
    summary = {"schema_version": 3, "passed": False, "hardware_verified": False,
               "mode": "host_offline_replay", "config": asdict(processing),
               "runtime": asdict(runtime), "versions": {"rm27_tracking": __version__,
               "python": platform.python_version(), "opencv": cv2.__version__, "numpy": np.__version__},
               "platform": {"machine": platform.machine(), "system": platform.system(), "release": platform.release()},
               "p95_method": "all-sample log histogram, upper bucket edge, 1.03 ratio",
               "percentile_method": "all-sample log histogram, upper bucket edge, 1.03 ratio",
               "timestamp_boundaries_clock": "current_host_monotonic_ns",
               "publication_semantic": "selected frame JSONL flush and first preview write returned; not disk durability",
               "camera_to_output_latency": None,
               "camera_to_output_unavailable_reason": "no verified source/exposure to current host clock relationship",
               "host_residence_semantic": "current host receipt to selected output completion; publication samples only",
               "frame_count": 0}
    summary.update(metadata or {})
    count = 0
    sample_start = rss_bytes()
    rss_first = rss_last = rss_min = rss_max = None
    sample_count = 0
    before_cpu = process_time_ns()
    started = perf_counter_ns()
    deadline_ns = started + int(runtime.duration_s * 1e9) if runtime.duration_s is not None else None
    last_sample = started
    consumer_validation = TimestampValidator()
    failure = None
    pixel_digest = hashlib.sha256()
    publication_count = 0
    try:
        packets = iter(buffered)
        with (output / 'frames.jsonl').open('x') as log, (output / 'resources.jsonl').open('x') as resources, \
                (output / 'publications.jsonl').open('x') as publications:
            while processing.max_frames is None or count < processing.max_frames:
                if stop_requested is not None and stop_requested():
                    summary["stop_reason"] = "requested"
                    break
                if runtime.duration_s is not None and (perf_counter_ns() - started) / 1e9 >= runtime.duration_s:
                    summary['stop_reason'] = 'duration'
                    break
                try:
                    packet = buffered.get(deadline_ns)
                except RunDeadline:
                    summary["stop_reason"] = "duration"
                    break
                except StopIteration:
                    summary['stop_reason'] = getattr(source, 'stats', {}).get('stop_reason', 'decoder_end')
                    break
                frame = packet.frame
                arrived = monotonic_ns()
                stages['queue_wait'].add(arrived - packet.queued_ns)
                consumer_validation.observe(frame)
                validated = monotonic_ns()
                image = preprocess(frame.image, processing, borrow_input=borrow_input)
                preprocessed = monotonic_ns()
                result = (processor(image) if frame_processor is None
                          else frame_processor(frame, image, buffered.summary))
                processed = monotonic_ns()
                stages['validation'].add(validated - arrived)
                stages['preprocessing'].add(preprocessed - validated)
                stages['processing'].add(processed - preprocessed)
                stages['preprocess_and_process'].add(processed - validated)
                stages['host_to_processing_complete'].add(processed - frame.host_received_monotonic_ns)
                logged = runtime.log_every and count % runtime.log_every == 0
                frame_hash = None
                if runtime.hash_frames:
                    # Aggregate covers EVERY processed output, independent of log sampling.
                    frame_hash = hashlib.sha256(image).hexdigest()
                    pixels = hashlib.sha256(json.dumps(list(image.shape)).encode('ascii') + image.dtype.str.encode('ascii'))
                    pixels.update(image)
                    pixel_digest.update(pixels.digest())
                output_start = monotonic_ns()
                if logged:
                    row = {"sequence": frame.sequence, "source_sequence": frame.source_sequence,
                           "stream_id": frame.stream_id, "timestamp_ns": frame.timestamp_ns,
                           "clock_domain": frame.clock_domain, "timestamp_semantic": frame.timestamp_semantic,
                           "received_monotonic_ns": frame.received_monotonic_ns,
                           "original_received_monotonic_ns": frame.original_received_monotonic_ns,
                           "replay_received_monotonic_ns": frame.replay_received_monotonic_ns,
                           "acquisition_metadata": frame.acquisition_metadata,
                           "boundaries": {"validation_start_monotonic_ns": arrived,
                                          "preprocessing_start_monotonic_ns": validated,
                                          "preprocessing_end_monotonic_ns": preprocessed,
                                          "processing_start_monotonic_ns": preprocessed,
                                          "processing_end_monotonic_ns": processed,
                                          "output_start_monotonic_ns": output_start},
                           "input_shape": list(frame.image.shape), "output_shape": list(image.shape),
                           "acquisition_ns": packet.acquisition_ns, "queue_depth_after_dequeue": packet.queue_depth,
                           "processing_ns": processed - validated, "result": result}
                    if frame_hash is not None:
                        row['processed_sha256'] = frame_hash
                    log.write(json.dumps(row, allow_nan=False) + '\n')
                    log.flush()
                if count == 0 and not cv2.imwrite(str(output / 'preview.png'), image):
                    raise RuntimeError('failed to write preview')
                published = monotonic_ns()
                if logged or count == 0:
                    # The event follows completion of the result operation it describes.
                    publications.write(json.dumps({"sequence": frame.sequence, "stream_id": frame.stream_id,
                                                   "output_start_monotonic_ns": output_start,
                                                   "published_monotonic_ns": published,
                                                   "operations": (["frame_jsonl_flush"] if logged else []) +
                                                                 (["preview_write"] if count == 0 else []),
                                                   "disk_durability": False}) + '\n')
                    publications.flush()
                    publication_count += 1
                    stages['host_residence'].add(published - frame.host_received_monotonic_ns)
                finished = monotonic_ns()
                stages['logging'].add(finished - processed)
                if count == 0:
                    summary.update(clock_domain=frame.clock_domain, timestamp_semantic=frame.timestamp_semantic)
                count += 1
                sample_now = perf_counter_ns()
                if sample_count == 0 or sample_now - last_sample >= 1_000_000_000:
                    rss = rss_bytes()
                    cpu = process_time_ns() - before_cpu
                    resources.write(json.dumps({'elapsed_s': (sample_now-started)/1e9,
                                                'rss_bytes': rss, 'cpu_time_ns': cpu}) + '\n')
                    if rss is not None:
                        rss_first = rss if rss_first is None else rss_first
                        rss_last = rss
                        rss_min = rss if rss_min is None else min(rss_min, rss)
                        rss_max = rss if rss_max is None else max(rss_max, rss)
                    sample_count += 1
                    last_sample = sample_now
            else:
                summary['stop_reason'] = 'max_frames'
        if not count and summary.get('stop_reason') != 'recording_byte_limit':
            raise ValueError('no frames processed')
    except BaseException as exc:
        failure = exc
    finally:
        try:
            buffered.close()
        except BaseException as exc:
            if failure is None:
                failure = exc
        if buffered.error is not None and failure is None:
            failure = buffered.error
        elapsed = perf_counter_ns() - started
        cpu = process_time_ns() - before_cpu
        queue_summary = buffered.summary()
        stage_summary = {name: values.summary() for name, values in stages.items()}
        pre = stage_summary['preprocessing']['mean_ms']
        proc = stage_summary['processing']['mean_ms']
        summary.update(frame_count=count, elapsed_ns=elapsed,
                       processed_frames_sha256=pixel_digest.hexdigest() if runtime.hash_frames else None,
                       processed_frames_hash_semantic='SHA256 of ordered SHA256(shape JSON + dtype.str + pixel bytes) digests',
                       publication_count=publication_count,
                       throughput_fps=count*1e9/elapsed if elapsed else 0,
                       processing_fps=1000/(pre+proc) if pre is not None and pre+proc else None,
                       cpu_utilization_percent=cpu/elapsed*100 if elapsed else 0,
                       cpu_time_ns=cpu,
                       rss={'before_bytes': sample_start, 'first_bytes': rss_first, 'last_bytes': rss_last,
                            'min_bytes': rss_min, 'max_bytes': rss_max,
                            'growth_bytes': rss_last-rss_first if rss_last is not None else None,
                            'samples': sample_count,
                            'process_high_water_bytes': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*1024},
                       stages=stage_summary, capture=queue_summary,
                       camera_reported=getattr(source, 'reported', None), camera_stats=getattr(source, 'stats', None))
        summary['acquisition_mean_ns'] = queue_summary['acquisition']['mean_ms'] * 1e6 if queue_summary['captured'] else None
        # Retain S1 summary aliases while richer stage metrics become authoritative.
        summary['processing_mean_ns'] = (pre+proc)*1e6 if pre is not None else None
        summary['processing_max_ns'] = stages['preprocess_and_process'].maximum if count else None
        summary['passed'] = failure is None
        if failure is not None:
            summary['error'] = str(failure)
        (output / 'summary.json').write_text(json.dumps(summary, indent=2, allow_nan=False)+'\n')
    if failure is not None:
        raise failure
    return summary
