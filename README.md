# rm27_tracking

Lightweight general-purpose vision acquisition, preprocessing and measurement
for future RK3576 Linux research. S2 extends the S1 package and CLI: the same
runner handles camera and file sources. No target engagement, armor selection,
collision guidance, attack-related reacquisition or actuator output is included.

## Install, build and test

Python 3.10+, NumPy >=1.24 and OpenCV (`cv2`, tested with 4.6 and 5.0).
Linux is the current runtime platform. There is no extra camera SDK dependency.
The pip OpenCV wheel is optional so a BSP/distro build can supply `cv2`.
Choose **one** OpenCV distribution; installing multiple packages that own `cv2`
can replace the BSP build and its codecs/backends.

For a development PC with pip-provided OpenCV:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install '.[opencv]'
python -m unittest discover -s tests -v
python -m pip wheel --no-deps . --wheel-dir dist
python scripts/check_platform.py
```

Without installation on this workstation:

```bash
PYTHONPATH=src /home/shiuhou/venvs/mujoco/bin/python -m unittest discover -s tests -v
PYTHONPATH=src /home/shiuhou/venvs/mujoco/bin/python -m rm27_tracking.replay \
  /absolute/path/to/video.mp4 --config configs/replay.json --out artifacts/replay-001
```

The platform probe reads versions/build flags only; it never opens a device.
`gst-launch-1.0` being installed does not establish Python OpenCV GStreamer
support. The selected venv reports GStreamer:NO, distro OpenCV reports YES.
S2 uses direct V4L2 because OpenCV capture does not expose the required kernel
sequence numbers, source timestamps and buffer lifecycle. A second GStreamer
capture implementation would duplicate the pipeline; add one only if the BSP
requires a multiplanar/ISP path that the current adapter cannot support.

## One processing path

`FrameSource → bounded acquisition queue → timestamp validation → preprocess
→ processor(image) → results / preview / JSONL / resource metrics`

- `pipeline.py` retains S1 `Frame`, `ReplaySource`, `ProcessingConfig` and
  `preprocess`. `Frame` adds `stream_id` and optional `source_sequence`.
- `camera.py` provides direct single-planar V4L2 MMAP capture; `formats.py`
  converts negotiated payloads to owned uint8 BGR arrays.
- `runtime.py::run_pipeline` is shared by replay, camera and benchmarks.
  `processor` is a callable receiving the processed image. The default generic
  processor reports channel mean/stddev using OpenCV, with no robotics semantics.
- `replay.py` retains `rm27-replay`; `capture.py` adds explicit `rm27-vision`;
  `benchmark.py` adds `rm27-benchmark` and reuses the actual file decoder.

`preprocess()` returns pixels independent of the input. An OpenCV transformation
already owns its output, so S2 removes the redundant final copy in transformed
paths. File decoder outputs are owned already; the extra S1 decoder copy is also
removed. MMAP data is converted/copied before QBUF returns a buffer to the driver.
Zero-copy hardware/DMABUF/RGA integration is outside this baseline.

Application queue capacity is configurable (1–64, default 2). `block` preserves
file order through backpressure; `latest` evicts the oldest queued frame when
processing falls behind and counts that drop. Memory holds at most the configured
queued frames plus the producer/consumer working frames and fixed-size metrics.
Kernel buffers are a separate bounded pool (2–32, requested default 4). The
reported buffer count is not a measurement of the driver's instantaneous queue
occupancy. Application high-water depth is measured.

The producer validates **all acquired frames before any queue drop**. Validation
requires strictly increasing application sequence/source timestamps and stable
clock provenance within an epoch. Host receipt may not regress. A reconnect
starts a new stream epoch, allowing a driver clock/sequence reset without forging
a continuous source timeline. Unknown clock domains remain unknown; the runner
does not transform a driver timestamp into exposure time. Duplicate, zero-only
or regressing source timestamps fail validation rather than silently switching
clocks. Finite run deadlines cancel stalled acquisition; read/retry waits are
interruptible. Worker shutdown is checked and a stuck decoder is a run failure.
Kernel ioctls depend on the BSP and are not guaranteed to be interruptible by
Python cancellation; this needs real hardware testing.

## Replay and timestamp provenance

```bash
rm27-replay /absolute/path/to/images --config configs/replay.json --out artifacts/replay-001
rm27-replay /absolute/path/to/video.mp4 --config configs/replay.json --out artifacts/replay-002
```

Image, image-directory and video inputs are read-only. Directory image order is
lexicographic (zero-pad numeric filenames); PNG/JPEG/BMP/TIFF are supported and
subdirectories/unsupported extensions are ignored. Output must be a new directory
outside the input directory. Corrupt selected images, invalid config and empty
sources fail. OpenCV cannot reliably distinguish video EOF from a midstream
codec failure: `decoder_end` is not proof of complete encoded-stream integrity.
Compare decoded count against external expected counts when completeness matters.
`max_frames` in the replay config validates only the selected prefix; background
acquisition is capped to that same prefix in the default `block` mode.

Replay timestamps remain `round(sequence * 1e9 / configured_fps)`, domain
`replay`, semantic `synthetic_sequence_fps`. Container PTS, variable-rate timing
and exposure timestamps are not inferred. `received_monotonic_ns` records host
receipt separately. `configs/replay.json` contains processing choices, not camera
mode claims: width/height together resize (aspect ratio can change), `grayscale`
converts BGR to gray, and optional `max_frames` bounds the run. Unknown keys fail.

## Linux camera and RK3576 configuration

No physical camera was opened during S2. After device identification, edit a
copy of `configs/rk3576.example.json`, then run explicitly on the camera host:

```bash
rm27-vision --config configs/rk3576.example.json --out artifacts/camera-001
```

This nested JSON has `camera`, `processing`, `runtime` sections. Device path,
width/height, FPS and FOURCC are configurable. Optional camera mode values are
null by default because no sensor/mode has been verified. `processing.fps` is a
replay clock setting; it does not command a camera (use `camera.fps`). Requests
may be adjusted by drivers; `camera_requested` and `camera_reported` distinguish
requested parameters from negotiated width, height, stride, format, FPS,
colorimetry, driver and kernel buffer count. Rejected/unsupported formats fail.
`configs/camera.example.json` remains a valid camera-only Python API config;
the live CLI uses the nested S2 file. `CameraConfig`/`CameraSource` remain
importable from `pipeline` for S1 callers.

Supported progressive linear single-planar formats: **NV12, NV21, YUYV, UYVY,
BGR3, RGB3, GREY, MJPG**. Padded row strides are respected. YUV conversion uses
OpenCV BT.601 limited range; driver-declared other/unknown colorimetry is rejected
rather than misconverted. Multiplanar-only nodes (including NM12/NM21), tiled
layouts, interlaced frames and ISP/media-graph setup are currently unsupported.
A real RK3576 BSP may expose only such paths; that is a deployment blocker,
not a reason to claim the current adapter has been qualified.

Each frame carries kernel `source_sequence`, raw buffer timeval scaled to ns,
and timestamp flags: clock domain is `v4l2_monotonic` only when flagged, otherwise
`v4l2_unknown`. Boundary semantics are driver end-of-frame, driver-reported
start-of-exposure **unverified**, or unknown. They are not exposure/synchronization
qualification. The application `sequence` stays increasing through restarts.
Kernel sequence gaps count `source_drops` (including uint32 wrap handling).
Application queue evictions are counted separately as `queue_drops`; stops may
also leave `discarded_on_stop`. Gaps across restarts are unknowable and excluded.

A disconnect/read timeout closes MMAP buffers and the fd, then consumes one of
`restart_attempts` (total budget per run, default 0). `restart_delay_s` is finite
and interruptible; exhausted retries fail with an error. The example opts into
two retries, a 2-second read timeout and a 60-second overall duration. The live
CLI requires a duration or frame cap. Direct API consumers must call `close()`
in `finally` when they stop early. Sources are single-use. Sensor exposure/gain,
triggering, hardware sync and automatic camera probing are not implemented.

## Reproducible benchmarks and measurements

```bash
rm27-benchmark --seconds 15 --out artifacts/bench-throughput
rm27-benchmark --seconds 60 --paced --queue-policy latest --out artifacts/bench-sustained
```

A deterministic moving gradient is encoded into a 60-frame 640×480 MJPEG fixture
and read repeatedly using `ReplaySource`. Each loop is a new replay epoch;
the optional pace is nominal 60 FPS. Processing is 320×240 grayscale plus
channel statistics. Fixture generation/hashing is outside the run timing.
The generated AVI hash and decoder versions are recorded; different codec
versions can produce different fixture bytes. These are synthetic host workloads.

Runs write `summary.json`, sampled `frames.jsonl`, `resources.jsonl` and the first
processed `preview.png`. Replay additionally writes ordered `inputs.jsonl`
paths/hashes. Keep source files immutable during a run. Frame logging frequency
and processed-pixel hashing are configurable; frame logs grow with the chosen
sample rate, resources are sampled at most once per second. No per-frame images
are retained. Runtime statistics use constant memory for arbitrary run length.

Summary metrics include acquisition throughput/latency, preprocessing and generic
processor throughput, stage means/max/P95, queue wait/depth/drops, host residence,
CPU time/utilization and RSS samples. Acquisition duration includes source decode
or MMAP format conversion; in a paced benchmark it also includes deliberate wait.
Processing FPS is compute-only reciprocal of mean preprocess+processor time.
Wall throughput includes pipeline I/O and instrumentation. CPU utilization is
process CPU / wall time ×100; more than 100% means more than one CPU core's time.
Linux `/proc/self/statm` supplies sampled RSS; process-lifetime `ru_maxrss` is
reported separately. P95 uses an all-sample log histogram with 1.03 bucket ratio
(upper-edge approximation error <=3% of latency plus 1 µs). Host residence starts
at application receipt and is **not sensor-to-result latency**. No stage compares
unrelated driver and host clock domains.

Host results on 2026-10-29 (America/New_York), x86_64 Linux, Python 3.12.3,
OpenCV 5.0.0 / NumPy 2.5.1, one OpenCV thread, application queue capacity 2:

| Synthetic replay run | Frames | Wall FPS | Preprocess+processor P95 | CPU | Max sampled RSS | Queue drops |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Unpaced 15 s | 21,560 | 1,437.20 | 0.186 ms | 136.73% | 68.82 MiB | 0 |
| Paced 60 s | 3,601 | 60.02 | 0.251 ms | 7.11% | 68.15 MiB | 0 |

Evidence: `artifacts/s2-throughput/run/summary.json` and
`artifacts/s2-sustained/run/summary.json` (local ignored generated data).
Paced RSS grew 1.23 MiB from the first sample; after 10 s its sampled range was
68.00–68.15 MiB (0.148 MiB growth). This is a short resource-stability observation,
not a leak-free guarantee. The two host runs overlapped for the first 15 seconds;
these are observed results, not an isolated optimization comparison. The bounded
queue overflow test deliberately uses a slow processor and verifies drop counts
and high-water depth. Actual camera FPS/drops, RK3576 CPU/RAM and thermal
stability have **not been measured**.

## ARM64 native setup and validation gate

No usable ARM64 sysroot/AArch64 GCC/QEMU or RK3576 execution environment was
available. Clang's presence alone cannot validate target dependencies or runtime.
This project builds a pure-Python wheel; that portability tag is not an ARM64
camera test. No ARM64 cross-build or board execution is claimed.

For an Ubuntu/Debian-like ARM64 BSP that supplies suitable packages, prospective
native commands are below (not executed in this task; package names/versions and
permissions must match the selected BSP):

```bash
sudo apt install python3-venv python3-opencv python3-numpy python3-setuptools python3-wheel build-essential linux-libc-dev
python3 -m venv --system-site-packages .venv
. .venv/bin/activate
python scripts/check_platform.py
python -m pip install --no-deps --no-build-isolation .
python -m unittest discover -s tests -v
python -m pip wheel --no-deps --no-build-isolation . --wheel-dir dist
rm27-benchmark --seconds 60 --paced --queue-policy latest --out artifacts/arm64-replay-001
# After selecting/verifying camera node and supported modes:
rm27-vision --config configs/rk3576.example.json --out artifacts/arm64-camera-001
```

The native test suite compiles a tiny probe against **that host's** Linux headers
to compare ctypes sizes/ioctl constants. This passed on x86_64 only; rerun on
ARM64 to validate its ABI. Run recorded-media replay before camera tests, then a
longer configured camera duration under representative image-processing load.
Keep negotiated format, source clock provenance, expected vs observed frame rate,
CPU/RSS/queue measurements and board/OS/driver versions with the run evidence.

RKNN preparation is deferred: there are no models/inference implementations in
this repository to adapt. A future optional backend needs a chosen model,
pre/postprocess contract and matched RKNN conversion/runtime versions. The CPU
pipeline remains independent of RKNN, and there are no invented NPU measurements.

## Existing code review and reuse decision

Inspected parent `rm27_drones` and clean third-party submodule
`rm27/perception/vision/maixcam2_dart_vision` at
`ac83e8c224214bc99483f9617450e110c5c16022`. Its origin remains
`https://github.com/blademaster679/maixcam2_dart_vision.git`.
Paths below are relative to that third-party repository unless noted.

| Existing component | Reuse assessment |
| --- | --- |
| `tools/video_replay/maix_image_opencv.cpp` | Uses OpenCV image operations behind a Maix compatibility layer, with blob/target-specific behavior. Reuse OpenCV primitives directly; do not copy the compatibility implementation. |
| `tools/video_replay/video_replay.cpp`, `CMakeLists.txt` | Existing host replay links detector, target fusion and motion code. Reuse the file-decoding approach without importing these dependencies. |
| `main/include/dart/async_frame.hpp` | Generic owned-frame metadata, latest-frame slot and timestamp scheduler are future C++ extraction candidates. The S2 Python runtime supplies its own small bounded producer queue; no C++ port is needed. |
| `tools/os04a10_highfps/frame_continuity.hpp` | Portable sequence-gap and PTS checks; useful once verified hardware sequence/clock metadata exists. Application replay indices cannot stand in for sensor counters. |
| `main/src/config.cpp` | Parser depends on detector settings. Standard-library JSON replaces that dependency for generic configuration. |
| Parent `rm27/perception/vision/localization/manifest.py` | Explicit clock semantics, provenance and exposure-verification rules are reusable design conventions. Do not relocate or change localization infrastructure for this task. |
| Parent `rm27/perception/vision/localization/analyze_video.py` | Uses OpenCV, NumPy, hashes and offline diagnostic outputs. Reuse those underlying libraries; its VSL qualification schema and external-tool workflow stay with localization. |
| Existing target JSON/log evaluators | Target-specific schemas are excluded. Small JSONL frame/measurement records use the standard library. |

No third-party source or historical artifacts were copied, moved or deleted.
No top-level license was found in the inspected third-party checkout; any future
source extraction needs provenance/license review. This baseline reuses the
existing OpenCV/NumPy dependencies rather than duplicating image algorithms.
No reusable source package could be imported independently without coupling to
vendor or localization code; the new code is the small acquisition/metadata/CLI
adapter. No software license has been selected for this repository.

## Parent integration and publication state

This is an independently versioned repository on `main`, with `origin` configured
as `https://github.com/shiuhou/rm27_tracking.git` at the owner's request.
The initial commit contains the S1/S2 source, configurations, tests and build
instructions. Generated media, benchmark logs, virtual environments, build
outputs and local engineering plans/run records remain ignored.

Remote existence, authentication and the first push still require verification:
network access was blocked during publication preparation. No credentials are
stored in this repository; GitHub access must use the owner's existing auth.

The parent `.gitmodules` retains an **inactive commented template** for
`components/rm27_tracking`. Publishing this child does not activate the parent
integration. A usable submodule needs a published child commit and a parent
mode-160000 gitlink. Register/pin the selected child commit in a separately
reviewed parent change, then verify a fresh recursive clone.
`git submodule absorbgitdirs components/rm27_tracking` may then normalize
Git-directory storage. This package builds and runs without the parent.

## Validation and remaining hardware requirements

S1's five tests passed before edits. S2 preserves its image/video ordering,
preprocessing, failure reporting and CLI through the common runner; its obsolete
OpenCV camera mock test was replaced by production V4L2 lifecycle tests.
Current suite: **23 tests PASS** under both OpenCV 5.0/NumPy 2.5.1 and distro
OpenCV 4.6/NumPy 1.26.4. Tests exercise real generated video CLI flow, image
prefix limits, deterministic hashes/order, timestamps/provenance/restart epochs,
known YUV colors and padding, MMAP ownership/requeue/partial-init cleanup, native
UAPI ABI, bounded queues/drops, processor failures and cancellation during stalls.
No physical camera was opened. The 0.2.0 wheel builds and installs outside the checkout; all 23 tests also
pass from that installation. All three console entry points pass CLI checks;
the installed benchmark runs and invalid live configuration fails before any
device open. The owner subsequently authorized the initial commit and publication;
the parent gitlink remains a separate integration change.

Remaining RK3576 requirements:

- Identify board/BSP/ARM64 ABI, sensor, connection, media graph and driver node.
- Verify negotiated progressive single-planar mode, colorimetry/stride, ISP output;
  add a multiplanar adapter only if the real BSP requires it.
- Measure source clock semantics, timestamp quality, exposure/shutter behavior,
  sequence gaps and synchronization. Buffer flags alone are insufficient evidence.
- Validate physical disconnect/reconnect, ioctl cancellation and sustained camera
  capture under load; characterize sensor-to-result latency with a common clock.
- Validate actual CPU/RSS/thermal behavior, codecs, calibration and recorded datasets.
  Hardware codec/RGA/RKNN, DMA-BUF and zero-copy integration remain unimplemented.

Implementation references are the selected host's `/usr/include/linux/videodev2.h`
and Linux UAPI buffer/colorimetry/MMAP contracts. Host header tests do not certify
BSP behavior. Useful upstream references:

- Linux V4L2 buffer contract: https://docs.kernel.org/userspace-api/media/v4l/buffer.html
- Linux MMAP streaming: https://docs.kernel.org/userspace-api/media/v4l/mmap.html
- OpenCV packaging (choose one cv2 provider): https://pypi.org/project/opencv-python-headless/
- Future RKNN Toolkit2 scope: https://github.com/airockchip/rknn-toolkit2
