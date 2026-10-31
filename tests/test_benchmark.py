import hashlib
from pathlib import Path
import tempfile
from time import monotonic, monotonic_ns
import unittest
from unittest.mock import patch

import numpy as np

from rm27_tracking.benchmark import LoopReplay, make_fixture, run_benchmark
from rm27_tracking.pipeline import Frame, TimestampValidator


class BenchmarkTests(unittest.TestCase):
    def test_complete_video_passes_preserve_order_and_hashes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / 'input.avi'
            make_fixture(path, width=32, height=24, fps=30, frames=7)
            before = path.read_bytes()
            hashes = []
            for _ in range(2):
                source = LoopReplay(path, fps=30, loop=False)
                frames = list(source)
                self.assertEqual([frame.sequence for frame in frames], list(range(7)))
                self.assertEqual([frame.timestamp_ns for frame in frames], [round(i*1e9/30) for i in range(7)])
                hashes.append([hashlib.sha256(frame.image).hexdigest() for frame in frames])
                self.assertEqual(source.completed_passes, 1)
                self.assertTrue(source.stopped.is_set())
            self.assertEqual(*hashes)
            summaries = [run_benchmark(root / name, path, complete=True, fps=30) for name in ('first', 'second')]
            for name, summary in zip(('first', 'second'), summaries):
                self.assertEqual(summary['frame_count'], 7)
                self.assertEqual(summary['capture']['captured'], 7)
                self.assertEqual(summary['capture']['queue_drops'], 0)
                self.assertEqual(summary['stop_reason'], 'decoder_end')
                self.assertTrue(summary['passed'])
                self.assertEqual(summary['benchmark']['inputs'][0]['sha256'], hashlib.sha256(before).hexdigest())
                self.assertTrue(summary['benchmark']['implementation_sha256'])
                self.assertTrue((root / name / 'inputs.jsonl').is_file())
            self.assertEqual(summaries[0]['processed_frames_sha256'], summaries[1]['processed_frames_sha256'])
            self.assertTrue(summaries[0]['processed_frames_sha256'])
            self.assertEqual(path.read_bytes(), before)

    def test_pacing_preserves_frames_and_sets_current_receipt(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'input.avi'
            make_fixture(path, width=32, height=24, fps=25, frames=3)
            source = LoopReplay(path, fps=25, paced=True, loop=False)
            started = monotonic()
            frames = list(source)
            self.assertGreaterEqual(monotonic()-started, .075)
            self.assertEqual([frame.sequence for frame in frames], [0, 1, 2])
            for previous, current in zip(frames, frames[1:]):
                self.assertGreater(current.replay_received_monotonic_ns, previous.replay_received_monotonic_ns)
                self.assertEqual(current.received_monotonic_ns, current.replay_received_monotonic_ns)

    def test_loop_preserves_recorded_metadata_and_remaps_epochs(self):
        image = np.zeros((2, 2, 3), np.uint8)
        originals = [Frame(0, image, 20, 'driver', 'driver_eof', 9_000_000_000_000,
                           stream_id=0, source_sequence=10,
                           original_received_monotonic_ns=9_000_000_000_000,
                           replay_received_monotonic_ns=monotonic_ns(),
                           acquisition_metadata={'raw_driver_field': 123}),
                     Frame(1, image, 0, 'driver', 'driver_eof', 9_000_000_000_001,
                           stream_id=1, source_sequence=0,
                           original_received_monotonic_ns=9_000_000_000_001,
                           replay_received_monotonic_ns=monotonic_ns(),
                           acquisition_metadata={'raw_driver_field': 456})]
        class Source:
            def __iter__(self):
                return iter(originals)
            def close(self):
                pass
        with patch('rm27_tracking.benchmark.open_replay', side_effect=lambda *args: Source()):
            source = LoopReplay('not-opened-on-disk', loop=True)
            iterator = iter(source)
            try:
                frames = [next(iterator) for _ in range(5)]
            finally:
                source.close()
                iterator.close()
        self.assertEqual([frame.sequence for frame in frames], [0, 1, 2, 3, 4])
        self.assertEqual([frame.stream_id for frame in frames], [0, 1, 2, 3, 4])
        self.assertEqual([frame.timestamp_ns for frame in frames], [20, 0, 20, 0, 20])
        self.assertEqual([frame.source_sequence for frame in frames], [10, 0, 10, 0, 10])
        for index, frame in enumerate(frames):
            original = originals[index % 2]
            self.assertEqual(frame.received_monotonic_ns, original.received_monotonic_ns)
            self.assertEqual(frame.original_received_monotonic_ns, original.received_monotonic_ns)
            self.assertEqual(frame.acquisition_metadata['raw_driver_field'], original.acquisition_metadata['raw_driver_field'])
            self.assertEqual(frame.acquisition_metadata['benchmark_original_stream_id'], original.stream_id)
            self.assertEqual(frame.acquisition_metadata['benchmark_pass'], index // 2)
        validator = TimestampValidator()
        for frame in frames:
            validator.observe(frame)
        self.assertEqual(validator.streams, 5)
        self.assertNotIn('benchmark_pass', originals[0].acquisition_metadata)

    def test_bad_configuration_does_not_create_run(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for arguments in ({'fps': 0}, {'fps': 1e10}, {'seconds': -1},
                              {'complete': True, 'queue_policy': 'latest'}, {'width': 31}):
                with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                    run_benchmark(root / 'invalid', **arguments)
                self.assertFalse((root / 'invalid').exists())
            path = root / 'images'
            path.mkdir()
            with self.assertRaises(ValueError):
                run_benchmark(path / 'output', path)
            self.assertFalse((path / 'output').exists())


if __name__ == '__main__':
    unittest.main()
