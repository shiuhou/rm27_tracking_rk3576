#!/usr/bin/env python3
"""Describe the environment; optionally collect explicit read-only device queries."""
import argparse
import json
import os
import platform
from pathlib import Path
import selectors
import shutil
import signal
import sys
import subprocess
from time import monotonic


MAX_QUERY_BYTES = 64 * 1024
QUERY_TIMEOUT_S = 10


def query(command):
    """Bound command duration and retained output; preserve failures explicitly."""
    if shutil.which(command[0]) is None:
        return {'command': command, 'status': 'BLOCKED', 'reason': 'query tool is not installed'}
    retained = bytearray()
    total = 0
    process = None
    timed_out = False
    reason = None
    deadline = monotonic() + QUERY_TIMEOUT_S
    try:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   start_new_session=True)
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while selector.get_map():
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(command, QUERY_TIMEOUT_S)
                for key, _ in selector.select(remaining):
                    chunk = os.read(key.fileobj.fileno(), MAX_QUERY_BYTES)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        break
                    total += len(chunk)
                    retained.extend(chunk[:max(0, MAX_QUERY_BYTES-len(retained))])
        process.wait(timeout=max(0, deadline-monotonic()))
    except subprocess.TimeoutExpired:
        timed_out = True
    except OSError as exc:
        reason = str(exc)
    finally:
        if process is not None:
            if timed_out or process.poll() is None:
                # Kill the query session, including any children holding its
                # output pipe open, before reaping the direct subprocess.
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            process.wait()
            process.stdout.close()
    result = {'command': command,
              'status': 'PASS' if process is not None and not timed_out and reason is None and process.returncode == 0 else 'FAIL',
              'returncode': process.returncode if process is not None and not timed_out else None,
              'timed_out': timed_out, 'output': retained.decode(errors='replace'),
              'output_truncated': total > MAX_QUERY_BYTES}
    if reason is not None:
        result['reason'] = reason
    return result


def device_path(value, prefix):
    path = Path(value)
    if path.parent != Path('/dev') or not path.name.startswith(prefix) or not path.name[len(prefix):].isdigit():
        raise argparse.ArgumentTypeError(f'expected explicit /dev/{prefix}N path')
    return str(path)


def probe(devices=(), media=()):
    result = {
        "machine": platform.machine(),
        "platform": platform.platform(),
        "python": platform.python_version(),
        "python_executable": sys.executable,
        "rk3576_hardware_verified": False,
        "camera_accessed": bool(devices or media),
        "camera_streaming_started": False,
        "tools": {name: shutil.which(name) for name in (
            "aarch64-linux-gnu-gcc", "aarch64-linux-gnu-g++", "clang",
            "qemu-aarch64", "gst-launch-1.0", "v4l2-ctl")},
        "linux_videodev2_header": Path("/usr/include/linux/videodev2.h").is_file(),
    }
    try:
        import numpy as np
        import cv2
    except ImportError as exc:
        result["dependencies_available"] = False
        result["dependency_error"] = str(exc)
    else:
        result["dependencies_available"] = True
        result["numpy"] = np.__version__
        result["opencv"] = cv2.__version__
        build_lines = cv2.getBuildInformation().splitlines()
        result["opencv_video_backends"] = [
            line.strip() for line in build_lines
            if line.strip().startswith(("FFMPEG:", "GStreamer:", "v4l/v4l2:"))
        ]
        result["opencv_threads"] = cv2.getNumThreads()
    result['camera_queries'] = [query(['v4l2-ctl', '-d', path, '--all', '--list-formats-ext',
                                      '--get-fmt-video', '--get-parm', '--list-ctrls-menus'])
                                for path in devices]
    result['media_queries'] = [query(['media-ctl', '-p', '-d', path]) for path in media]
    result['device_query_scope'] = ('Explicit read-only capability/format/control/media queries; '
                                    'no format/control changes or streaming; no physical compatibility pass implied')
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--device', action='append', default=[],
                        type=lambda value: device_path(value, 'video'), help='explicit node to query; repeat for two cameras')
    parser.add_argument('--media', action='append', default=[],
                        type=lambda value: device_path(value, 'media'), help='explicit media graph to query')
    parser.add_argument('--out', type=Path, help='new evidence directory; default prints JSON')
    args = parser.parse_args(argv)
    if args.out is not None:
        args.out.mkdir(parents=True, exist_ok=False)
    result = probe(args.device, args.media)
    if args.out is not None:
        (args.out/'platform.json').write_text(json.dumps(result, indent=2)+'\n')
    else:
        print(json.dumps(result, indent=2))
    queries = result['camera_queries'] + result['media_queries']
    return 0 if result['dependencies_available'] and all(item['status'] == 'PASS' for item in queries) else 1


if __name__ == '__main__':
    sys.exit(main())
