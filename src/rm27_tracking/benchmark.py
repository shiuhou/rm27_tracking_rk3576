"""Reproducible synthetic-video benchmark using the production replay pipeline."""
import argparse
from dataclasses import replace
import json
from pathlib import Path
from threading import Event
from time import monotonic, monotonic_ns

import cv2
import numpy as np

from .pipeline import ProcessingConfig, ReplaySource, positive
from .replay import sha256_file
from .runtime import RuntimeConfig, run_pipeline


class LoopReplay:
    """Benchmark-only loop/pacing adapter around the actual file decoder.

    Each loop is a new stream epoch; no live sensor data or camera FPS is implied.
    Pacing may skip time slots but never fabricates source frame drops.
    """
    def __init__(self, path, fps=60, paced=False):
        self.path, self.fps, self.paced = path, fps, paced
        self.stopped = Event()
        self.current = None

    def __iter__(self):
        sequence = stream = 0
        origin = next_slot = monotonic()
        try:
            while not self.stopped.is_set():
                self.current = ReplaySource(self.path, self.fps)
                for frame in self.current:
                    if self.paced and self.stopped.wait(max(0, next_slot - monotonic())):
                        return
                    if self.stopped.is_set():
                        return
                    # Advance to the next future slot; after a stall, never
                    # burst stale work to catch up with missed wall-clock slots.
                    next_slot = origin + (int((monotonic()-origin)*self.fps)+1)/self.fps
                    yield replace(frame, sequence=sequence, stream_id=stream,
                                  received_monotonic_ns=monotonic_ns())
                    sequence += 1
                stream += 1
        finally:
            self.close()

    def close(self):
        self.stopped.set()
        if self.current is not None:
            self.current.close()


def make_fixture(path, width=640, height=480, fps=60, frames=60):
    """Small deterministic moving gradient, encoded with the host MJPEG encoder."""
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*'MJPG'), fps, (width, height))
    if not writer.isOpened():
        raise RuntimeError('MJPG encoder unavailable for benchmark fixture')
    y, x = np.indices((height, width), dtype=np.uint16)
    try:
        for sequence in range(frames):
            image = np.stack(((x+sequence*3)%256, (y+sequence*5)%256,
                              (x+y+sequence*7)%256), axis=-1).astype(np.uint8)
            writer.write(image)
    finally:
        writer.release()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True, help='new output directory')
    parser.add_argument('--seconds', type=float, default=15)
    parser.add_argument('--paced', action='store_true', help='pace decoded replay at nominal 60 FPS')
    parser.add_argument('--queue-policy', choices=['block', 'latest'], default='block')
    parser.add_argument('--width', type=int, default=640)
    parser.add_argument('--height', type=int, default=480)
    args = parser.parse_args(argv)
    try:
        positive(args.seconds, 'seconds')
        if args.width < 16 or args.height < 16 or args.width % 2 or args.height % 2:
            raise ValueError('fixture dimensions must be even and at least 16')
        args.out.mkdir(parents=True, exist_ok=False)
        fixture = args.out / 'fixture.avi'
        make_fixture(fixture, args.width, args.height)
        source = LoopReplay(fixture, paced=args.paced)
        runtime = RuntimeConfig(queue_policy=args.queue_policy, duration_s=args.seconds,
                                log_every=60, hash_frames=False, opencv_threads=1)
        processing = ProcessingConfig(width=320, height=240, grayscale=True)
        summary = run_pipeline(source, args.out / 'run', processing, runtime,
                              metadata={'mode': 'host_synthetic_video_benchmark',
                                        'fixture': {'path': str(fixture.resolve()), 'sha256': sha256_file(fixture),
                                                    'width': args.width, 'height': args.height,
                                                    'fps': 60, 'frames': 60, 'paced': args.paced}})
    except (Exception, KeyboardInterrupt) as exc:
        parser.exit(1, f'benchmark failed: {exc}\n')
    print(json.dumps(summary, allow_nan=False))


if __name__ == '__main__':
    main()
