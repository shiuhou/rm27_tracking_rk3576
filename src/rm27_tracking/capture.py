"""Explicit live camera entry point using the same processing runner as replay."""
import argparse
from dataclasses import asdict
import json
from pathlib import Path

from .camera import CameraConfig, CameraSource
from .pipeline import ProcessingConfig
from .recording import RecordingWriter
from .runtime import RuntimeConfig, run_pipeline


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True, help='new output directory')
    args = parser.parse_args(argv)
    recorder = None
    try:
        values = json.loads(args.config.read_text())
        if not isinstance(values, dict) or set(values) - {'camera', 'processing', 'runtime', 'recording'}:
            raise ValueError('camera config requires only camera/processing/runtime/recording objects')
        camera = CameraConfig(**values.get('camera', {}))
        processing = ProcessingConfig(**values.get('processing', {}))
        runtime = RuntimeConfig(**values.get('runtime', {}))
        if runtime.duration_s is None and processing.max_frames is None:
            raise ValueError('camera run requires duration_s or max_frames')
        recording = values.get('recording')
        if recording is not None:
            if not isinstance(recording, dict) or set(recording) != {'max_bytes'}:
                raise ValueError('recording requires only a positive max_bytes payload limit')
            if type(recording['max_bytes']) is not int or recording['max_bytes'] <= 0:
                raise ValueError('recording max_bytes must be a positive integer')
        args.out.mkdir(parents=True, exist_ok=False)
        if recording is not None:
            recorder = RecordingWriter(args.out / 'recording', recording['max_bytes'])
        result = run_pipeline(CameraSource(camera, recorder=recorder), args.out, processing, runtime,
                              metadata={'mode': 'linux_camera', 'camera_requested': asdict(camera)})
        if recorder is not None:
            recorder.finish(complete=True, reason=result['stop_reason'])
            result['recording'] = recorder.stats
            (args.out / 'summary.json').write_text(json.dumps(result, indent=2, allow_nan=False)+'\n')
    except (Exception, KeyboardInterrupt) as exc:
        if recorder is not None:
            try:
                recorder.finish(complete=False, reason=str(exc))
            except Exception:
                pass  # Retain the primary failure; manifest starts incomplete.
            summary_path = args.out / 'summary.json'
            if summary_path.is_file():
                try:
                    failed = json.loads(summary_path.read_text())
                    failed.update(passed=False, error=str(exc), recording=recorder.stats)
                    summary_path.write_text(json.dumps(failed, indent=2, allow_nan=False)+'\n')
                except (OSError, ValueError):
                    pass  # Storage may also prevent the diagnostic update.
        parser.exit(1, f'capture failed: {exc}\n')
    print(json.dumps(result, allow_nan=False))


if __name__ == '__main__':
    main()
