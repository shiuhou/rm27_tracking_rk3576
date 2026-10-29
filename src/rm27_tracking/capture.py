"""Explicit live camera entry point using the same processing runner as replay."""
import argparse
from dataclasses import asdict
import json
from pathlib import Path

from .camera import CameraConfig, CameraSource
from .pipeline import ProcessingConfig
from .runtime import RuntimeConfig, run_pipeline


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True, help='new output directory')
    args = parser.parse_args(argv)
    try:
        values = json.loads(args.config.read_text())
        if not isinstance(values, dict) or set(values) - {'camera', 'processing', 'runtime'}:
            raise ValueError('camera config requires only camera/processing/runtime objects')
        camera = CameraConfig(**values.get('camera', {}))
        processing = ProcessingConfig(**values.get('processing', {}))
        runtime = RuntimeConfig(**values.get('runtime', {}))
        if runtime.duration_s is None and processing.max_frames is None:
            raise ValueError('camera run requires duration_s or max_frames')
        args.out.mkdir(parents=True, exist_ok=False)
        result = run_pipeline(CameraSource(camera), args.out, processing, runtime,
                              metadata={'mode': 'linux_camera', 'camera_requested': asdict(camera)})
    except (Exception, KeyboardInterrupt) as exc:
        parser.exit(1, f'capture failed: {exc}\n')
    print(json.dumps(result, allow_nan=False))


if __name__ == '__main__':
    main()
