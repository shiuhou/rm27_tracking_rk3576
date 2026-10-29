#!/usr/bin/env python3
"""Describe the Python/OS vision environment without opening camera devices."""
import json
import platform
from pathlib import Path
import shutil
import sys


def probe():
    result = {
        "machine": platform.machine(),
        "platform": platform.platform(),
        "python": platform.python_version(),
        "python_executable": sys.executable,
        "rk3576_hardware_verified": False,
        "camera_accessed": False,
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
    return result


if __name__ == "__main__":
    print(json.dumps(probe(), indent=2))
