"""Headless replay CLI with bounded-memory metrics and reproducibility metadata."""
import argparse
import hashlib
import json
from pathlib import Path

import cv2

from .pipeline import ProcessingConfig, ReplaySource
from .runtime import RuntimeConfig, run_pipeline


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run(input_path, output_path, config):
    source = ReplaySource(input_path, config.fps)
    output = Path(output_path).resolve()
    # Avoid contaminating the source dataset or overwriting source files.
    if source.path == output or (source.path.is_dir() and source.path in output.parents):
        raise ValueError("output must be outside the input directory")
    output.mkdir(parents=True, exist_ok=False)
    try:
        inputs = source.files if source.files is not None else [source.path]
        with (output / "inputs.jsonl").open("x") as manifest:
            for path in inputs:
                manifest.write(json.dumps({"path": str(path), "sha256": sha256_file(path)}) + "\n")
    except BaseException:
        source.close()
        raise
    return run_pipeline(source, output, config, RuntimeConfig())


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="image, image directory (lexical order), or video file")
    parser.add_argument("--out", type=Path, required=True, help="new output directory")
    parser.add_argument("--config", type=Path)
    args = parser.parse_args(argv)
    try:
        values = json.loads(args.config.read_text()) if args.config else {}
        if not isinstance(values, dict):
            raise ValueError("configuration must be a JSON object")
        config = ProcessingConfig(**values)
        summary = run(args.input, args.out, config)
    except (OSError, ValueError, TypeError, RuntimeError, cv2.error) as exc:
        parser.exit(1, f"replay failed: {exc}\n")
    print(json.dumps(summary, allow_nan=False))


if __name__ == "__main__":
    main()
