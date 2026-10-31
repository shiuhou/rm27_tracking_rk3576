"""Reproducible file/recording and synthetic benchmarks through the shared runner."""
import argparse
from dataclasses import replace
import hashlib
import json
from pathlib import Path
from threading import Event
from time import monotonic, monotonic_ns

import cv2
import numpy as np

from .pipeline import ProcessingConfig, open_replay, positive
from .replay import sha256_file
from .runtime import RuntimeConfig, run_pipeline


class LoopReplay:
    """Finite or looping replay with optional host-wall pacing.

    All input frames are decoded in order. Pacing skips missed wall-clock slots,
    never input frames; no sensor-drop or exposure-timing inference is made.
    Original recording metadata remains attached when playback epochs are remapped.
    """
    def __init__(self, path, fps=60, paced=False, loop=True, allow_incomplete=False):
        positive(fps, 'fps')
        if fps > 1e9:
            raise ValueError('fps exceeds nanosecond resolution')
        self.path, self.fps, self.paced = Path(path).resolve(), fps, paced
        self.loop, self.allow_incomplete = loop, allow_incomplete
        self.stopped = Event()
        self.current = None
        self.started = False
        self.completed_passes = 0

    @property
    def reported(self):
        return getattr(self.current, 'reported', None)

    @property
    def stats(self):
        return {'replay_passes_completed': self.completed_passes,
                'input': getattr(self.current, 'stats', None)}

    def __iter__(self):
        if self.started or self.stopped.is_set():
            raise RuntimeError('source is closed or already consumed')
        self.started = True
        sequence_offset = stream_offset = 0
        origin = next_slot = monotonic()
        try:
            while not self.stopped.is_set():
                self.current = open_replay(self.path, self.fps, self.allow_incomplete)
                first_sequence = first_stream = last_sequence = last_stream = None
                for frame in self.current:
                    if first_sequence is None:
                        first_sequence, first_stream = frame.sequence, frame.stream_id
                    if self.paced and self.stopped.wait(max(0, next_slot - monotonic())):
                        return
                    if self.stopped.is_set():
                        return
                    now = monotonic()
                    # Wait until the next future slot after a stall, avoiding
                    # bursts while preserving every source frame in order.
                    next_slot = origin + (int((now-origin)*self.fps)+1)/self.fps
                    metadata = dict(frame.acquisition_metadata or {})
                    metadata.update(benchmark_original_sequence=frame.sequence,
                                    benchmark_original_stream_id=frame.stream_id,
                                    benchmark_pass=self.completed_passes)
                    delivered = monotonic_ns()
                    recorded = frame.original_received_monotonic_ns is not None
                    yield replace(frame, sequence=sequence_offset+frame.sequence,
                                  stream_id=stream_offset+frame.stream_id,
                                  received_monotonic_ns=frame.received_monotonic_ns if recorded else delivered,
                                  replay_received_monotonic_ns=delivered,
                                  acquisition_metadata=metadata)
                    last_sequence, last_stream = frame.sequence, frame.stream_id
                self.current.close()
                if last_sequence is None:
                    raise ValueError('benchmark input contains no frames')
                self.completed_passes += 1
                if not self.loop:
                    return
                sequence_offset += last_sequence + 1 - first_sequence
                stream_offset += last_stream + 1 - first_stream
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


def implementation_hashes():
    """Identify the actual installed Python implementation, including local edits."""
    return {path.name: sha256_file(path) for path in sorted(Path(__file__).parent.glob('*.py'))}


def run_benchmark(output, input_path=None, seconds=15, complete=False, paced=False,
                  fps=60, queue_policy='block', width=640, height=480,
                  allow_incomplete=False):
    positive(fps, 'fps')
    if fps > 1e9:
        raise ValueError('fps exceeds nanosecond resolution')
    if not complete:
        positive(seconds, 'seconds')
    if complete and queue_policy != 'block':
        raise ValueError('complete deterministic passes require queue_policy=block')
    runtime = RuntimeConfig(queue_policy=queue_policy, duration_s=None if complete else seconds,
                            log_every=60, hash_frames=True, opencv_threads=1)
    processing = ProcessingConfig(fps=fps, width=320, height=240, grayscale=True)
    output = Path(output).resolve()
    input_path = Path(input_path).resolve() if input_path is not None else None
    if input_path is not None and (input_path == output or
                                    (input_path.is_dir() and input_path in output.parents) or
                                    (input_path.name == 'manifest.json' and (input_path.parent == output or input_path.parent in output.parents))):
        raise ValueError('output must be outside the input recording/image directory')
    if input_path is None and (type(width) is not int or type(height) is not int or
                              width < 16 or height < 16 or width % 2 or height % 2):
        raise ValueError('fixture dimensions must be even and at least 16')
    output.mkdir(parents=True, exist_ok=False)
    synthetic = input_path is None
    if synthetic:
        input_path = output / 'fixture.avi'
        make_fixture(input_path, width, height, fps)
    # This constructor only enumerates images or reads a recording manifest. All
    # source hashes are computed before opening any decoder or reading pixels.
    source_info = open_replay(input_path, fps, allow_incomplete)
    try:
        files = source_info.files if source_info.files is not None else [source_info.path]
        inputs = [{'path': str(path), 'bytes': path.stat().st_size,
                   'sha256': sha256_file(path)} for path in files]
    finally:
        source_info.close()
    with (output / 'inputs.jsonl').open('x') as manifest:
        for item in inputs:
            manifest.write(json.dumps(item) + '\n')
    implementation = implementation_hashes()
    metadata = {'mode': 'host_synthetic_video_benchmark' if synthetic else 'host_recorded_input_benchmark',
                'benchmark': {'input': str(input_path), 'inputs': inputs,
                              'input_provenance': 'generated moving-gradient MJPEG fixture' if synthetic else
                                  'existing recorded input; sensor/exposure/drop provenance not inferred',
                              'complete_pass': complete, 'paced': paced,
                              'pacing_fps': fps if paced else None,
                              'replay_timeline_fps': fps,
                              'implementation_files_sha256': implementation,
                              'implementation_sha256': hashlib.sha256(json.dumps(implementation, sort_keys=True).encode()).hexdigest()}}
    if synthetic:
        metadata['fixture'] = {'path': str(input_path), 'sha256': inputs[0]['sha256'],
                               'width': width, 'height': height, 'fps': fps, 'frames': 60, 'paced': paced}
    source = LoopReplay(input_path, fps=fps, paced=paced, loop=not complete,
                        allow_incomplete=allow_incomplete)
    return run_pipeline(source, output / 'run', processing, runtime, metadata=metadata)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True, help='new output directory')
    parser.add_argument('--input', type=Path, help='video, images, recording directory or manifest; omit for synthetic fixture')
    duration = parser.add_mutually_exclusive_group()
    duration.add_argument('--seconds', type=float, help='run duration; default 15 seconds, looping input as needed')
    duration.add_argument('--complete', action='store_true', help='one complete deterministic pass without a time limit')
    parser.add_argument('--fps', type=float, default=60, help='declared replay timeline and pacing rate; not sensor timing')
    parser.add_argument('--paced', action='store_true', help='pace replay at the declared --fps')
    parser.add_argument('--allow-incomplete', action='store_true', help='explicitly permit verified complete prefix of an incomplete recording')
    parser.add_argument('--queue-policy', choices=['block', 'latest'], default='block')
    parser.add_argument('--width', type=int, default=640, help='synthetic fixture width')
    parser.add_argument('--height', type=int, default=480, help='synthetic fixture height')
    args = parser.parse_args(argv)
    try:
        summary = run_benchmark(args.out, args.input, seconds=args.seconds if args.seconds is not None else 15,
                                complete=args.complete, paced=args.paced, fps=args.fps,
                                queue_policy=args.queue_policy, width=args.width, height=args.height,
                                allow_incomplete=args.allow_incomplete)
    except (Exception, KeyboardInterrupt) as exc:
        parser.exit(1, f'benchmark failed: {exc}\n')
    print(json.dumps(summary, allow_nan=False))


if __name__ == '__main__':
    main()
