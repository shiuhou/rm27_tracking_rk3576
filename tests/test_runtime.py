from dataclasses import replace
import json
from pathlib import Path
from threading import Event
import tempfile
from time import monotonic, monotonic_ns, sleep
import unittest

import numpy as np

from rm27_tracking.pipeline import Frame, ProcessingConfig, TimestampValidator
from rm27_tracking.runtime import BufferedSource, Latencies, RuntimeConfig, run_pipeline


class Frames:
    def __init__(self, count=20, delay=0):
        self.count, self.delay = count, delay
        self.closed = Event()

    def __iter__(self):
        for index in range(self.count):
            if self.closed.wait(self.delay):
                return
            yield Frame(index, np.full((12, 16, 3), index, np.uint8), index*1_000_000,
                        'synthetic', 'test_sequence_fps', monotonic_ns())

    def close(self):
        self.closed.set()


class RuntimeTests(unittest.TestCase):
    def test_timestamp_regression_provenance_and_restart(self):
        frame = Frame(0, np.zeros((2, 2, 3), np.uint8), 100, 'driver_unknown', 'driver_eof', 1000)
        validator = TimestampValidator()
        validator.observe(frame)
        for invalid in (replace(frame, sequence=1, timestamp_ns=99),
                        replace(frame, sequence=1, timestamp_ns=101, clock_domain='host'),
                        replace(frame, sequence=1, stream_id=-1)):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                validator.observe(invalid)
        validator.observe(replace(frame, sequence=1, timestamp_ns=0, stream_id=1))
        self.assertEqual(validator.streams, 2)

    def test_backpressure_preserves_order_and_joins(self):
        source = Frames()
        buffered = BufferedSource(source, capacity=2, policy='block')
        result = []
        try:
            for packet in buffered:
                result.append(packet.frame.sequence)
                sleep(.001)
        finally:
            buffered.close()
        self.assertEqual(result, list(range(20)))
        summary = buffered.summary()
        self.assertEqual(summary['captured'], 20)
        self.assertEqual(summary['queue_drops'], 0)
        self.assertLessEqual(summary['queue_high_water'], 2)
        self.assertFalse(buffered.thread.is_alive())

    def test_latest_drops_oldest_without_growing(self):
        source = Frames(count=30)
        buffered = BufferedSource(source, capacity=2, policy='latest')
        iterator = iter(buffered)
        self.assertTrue(source.closed.wait(2))  # producer exhausted, deterministic queue overflow
        result = []
        try:
            result = [packet.frame.sequence for packet in iterator]
        finally:
            buffered.close()
        self.assertEqual(result, [28, 29])
        self.assertEqual(buffered.summary()['queue_drops'], 28)
        self.assertEqual(buffered.summary()['queue_high_water'], 2)
        self.assertFalse(buffered.thread.is_alive())

    def test_invalid_frames_are_not_hidden_by_latest_drops(self):
        class Invalid(Frames):
            def __iter__(self):
                for frame in super().__iter__():
                    yield replace(frame, timestamp_ns=0)
        buffered = BufferedSource(Invalid(20), 1, 'latest')
        iterator = iter(buffered)
        try:
            with self.assertRaisesRegex(ValueError, 'timestamp'):
                while True:
                    next(iterator)
        finally:
            buffered.close()

    def test_pipeline_shared_processor_metrics_and_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Frames(count=80, delay=.0005)
            def process(image):
                sleep(.002)
                return {'value': int(image.flat[0])}
            summary = run_pipeline(source, Path(tmp), ProcessingConfig(grayscale=True),
                                   RuntimeConfig(queue_capacity=2, queue_policy='latest'), processor=process)
            self.assertTrue(summary['passed'])
            self.assertLessEqual(summary['capture']['queue_high_water'], 2)
            self.assertGreater(summary['capture']['queue_drops'], 0)
            self.assertEqual(summary['capture']['captured'], summary['frame_count'] + summary['capture']['queue_drops'])
            self.assertGreater(summary['stages']['processing']['p95_ms'], 1)
            self.assertGreater(summary['cpu_utilization_percent'], 0)
            self.assertGreater(summary['rss']['max_bytes'], 0)
            rows = [json.loads(line) for line in (Path(tmp)/'frames.jsonl').read_text().splitlines()]
            self.assertEqual([r['sequence'] for r in rows], sorted(r['sequence'] for r in rows))
            self.assertTrue(all(r['result']['value']==r['sequence'] for r in rows))
            self.assertTrue(source.closed.is_set())
            previous_summary = (Path(tmp)/'summary.json').read_bytes()
            with self.assertRaises(FileExistsError):
                run_pipeline(Frames(), Path(tmp), ProcessingConfig(), RuntimeConfig())
            self.assertEqual((Path(tmp)/'summary.json').read_bytes(), previous_summary)

    def test_processor_error_and_early_stop_release_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Frames()
            def fail(image):
                raise ValueError('processor failed')
            with self.assertRaisesRegex(ValueError, 'processor failed'):
                run_pipeline(source, Path(tmp), ProcessingConfig(), RuntimeConfig(), processor=fail)
            self.assertTrue(source.closed.is_set())
            self.assertFalse(json.loads((Path(tmp)/'summary.json').read_text())['passed'])
        buffered = BufferedSource(Frames(count=1000, delay=.01), capacity=1)
        iterator = iter(buffered)
        next(iterator)
        buffered.close()
        self.assertFalse(buffered.thread.is_alive())

    def test_deadline_cancels_stalled_source_and_close_failure_joins(self):
        class Stalled(Frames):
            def __iter__(self):
                self.closed.wait(10)
                return
                yield  # iterator that represents a cancellable blocked read
        with tempfile.TemporaryDirectory() as tmp:
            source = Stalled()
            started = monotonic()
            with self.assertRaisesRegex(ValueError, 'no frames'):
                run_pipeline(source, Path(tmp), ProcessingConfig(), RuntimeConfig(duration_s=.02))
            self.assertLess(monotonic()-started, 1)
            self.assertTrue(source.closed.is_set())
            self.assertEqual(json.loads((Path(tmp)/'summary.json').read_text())['stop_reason'], 'duration')
        class CloseFailure(Frames):
            def close(self):
                self.closed.set()
                raise RuntimeError('close failed')
        buffered = BufferedSource(CloseFailure(count=1000, delay=.001), capacity=1)
        next(iter(buffered))
        with self.assertRaisesRegex(RuntimeError, 'close failed'):
            buffered.close()
        self.assertFalse(buffered.thread.is_alive())
        self.assertEqual(len(buffered.queue), 0)

    def test_camera_restart_metadata_through_shared_runner(self):
        from unittest.mock import patch
        from rm27_tracking.camera import CameraConfig, CameraSource
        image = np.zeros((12, 16, 3), np.uint8)
        class Capture:
            instances = 0
            def __init__(self, config):
                self.epoch = Capture.instances
                Capture.instances += 1
                self.reported = {'buffer_count': config.buffer_count}
                self.frames = iter([10, 12] if self.epoch == 0 else [0, 1])
            def open(self):
                pass
            def read(self):
                try:
                    seq = next(self.frames)
                except StopIteration:
                    raise OSError('disconnect')
                return image.copy(), seq, seq*1000, 'v4l2_monotonic', 'driver_end_of_frame', monotonic_ns()
            def close(self):
                pass
        with tempfile.TemporaryDirectory() as tmp, patch('rm27_tracking.camera.V4L2Capture', Capture):
            summary = run_pipeline(CameraSource(CameraConfig(restart_attempts=1, restart_delay_s=.001)),
                                   Path(tmp), ProcessingConfig(max_frames=4), RuntimeConfig())
            self.assertTrue(summary['passed'])
            self.assertEqual(summary['capture']['streams'], 2)
            self.assertEqual(summary['camera_stats']['source_drops'], 1)
            rows = [json.loads(line) for line in (Path(tmp)/'frames.jsonl').read_text().splitlines()]
            self.assertEqual([r['stream_id'] for r in rows], [0, 0, 1, 1])
            self.assertEqual([r['source_sequence'] for r in rows], [10, 12, 0, 1])

    def test_latency_histogram_and_config_bounds(self):
        latency = Latencies()
        for value in range(1, 101):
            latency.add(value*1_000_000)
        summary = latency.summary()
        self.assertEqual(summary['count'], 100)
        self.assertAlmostEqual(summary['mean_ms'], 50.5)
        self.assertGreaterEqual(summary['p95_ms'], 95)
        self.assertLessEqual(summary['p95_ms'], 95*1.031)
        self.assertGreaterEqual(summary['p50_ms'], 50)
        self.assertLessEqual(summary['p50_ms'], 50*1.031)
        self.assertGreaterEqual(summary['p99_ms'], 99)
        self.assertLessEqual(summary['p99_ms'], 99*1.031)
        for values in ({'queue_capacity': 0}, {'duration_s': float('inf')}, {'hash_frames': 1}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                RuntimeConfig(**values)

    def test_replay_clock_epoch_and_publication_order_are_separate(self):
        class Recorded(Frames):
            def __iter__(self):
                for frame in super().__iter__():
                    yield replace(frame, timestamp_ns=frame.sequence*1000,
                                  clock_domain='v4l2_unknown', timestamp_semantic='driver_end_of_frame',
                                  received_monotonic_ns=10**19 + frame.sequence,
                                  original_received_monotonic_ns=10**19 + frame.sequence,
                                  replay_received_monotonic_ns=monotonic_ns(),
                                  acquisition_metadata={'exposure_controls': 'not synchronized'})
        with tempfile.TemporaryDirectory() as tmp:
            summary = run_pipeline(Recorded(3), Path(tmp), ProcessingConfig(), RuntimeConfig())
            rows = [json.loads(line) for line in (Path(tmp)/'frames.jsonl').read_text().splitlines()]
            events = [json.loads(line) for line in (Path(tmp)/'publications.jsonl').read_text().splitlines()]
            self.assertEqual(len(events), 3)
            for row, event in zip(rows, events):
                self.assertGreater(row['original_received_monotonic_ns'], row['replay_received_monotonic_ns'])
                boundaries = list(row['boundaries'].values())
                self.assertEqual(boundaries, sorted(boundaries))
                self.assertLessEqual(row['replay_received_monotonic_ns'], boundaries[0])
                self.assertLessEqual(boundaries[-1], event['published_monotonic_ns'])
                self.assertEqual(row['sequence'], event['sequence'])
                self.assertNotIn('published_monotonic_ns', row['boundaries'])
                self.assertFalse(event['disk_durability'])
            self.assertIsNone(summary['camera_to_output_latency'])
            self.assertEqual(summary['stages']['host_residence']['count'], 3)
            self.assertGreater(summary['stages']['host_residence']['mean_ms'], 0)
            self.assertLess(summary['stages']['host_residence']['max_ms'], 1000)
            self.assertEqual(len(summary['processed_frames_sha256']), 64)


if __name__ == '__main__':
    unittest.main()
