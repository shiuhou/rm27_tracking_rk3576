# rm27_tracking_rk3576

Lightweight general-purpose vision acquisition, preprocessing and measurement
for RM27’s active RK3576 Linux platform. The shared runner supports original-payload
recording/replay, calibration estimates and real-input benchmarks. S3.5 adds
tested one-memory-plane V4L2 multiplanar capture, calibration loading and ARM64
userspace verification. Mission-specific plate recognition and reacquisition,
contact guidance and actuator outputs belong to the separate mission layer.

Repository: `shiuhou/rm27_tracking_rk3576`; parent checkout path:
`components/rm27_tracking_rk3576/`. The Python distribution remains
`rm27-tracking`, imports remain `rm27_tracking`, and the four `rm27-*` commands
retain their existing names so installed callers continue to work.

As of 2026-11-11, RK3576 is the sole active RM27 onboard platform.
M3C is retired from development and deployment; its source, data, and validation
records remain historical references. Platform acceptance requires RK3576 evidence.
The former M3C reference in `rm27/perception/vision/rm27_tracking_m3c/` retains
its upstream and historical provenance. Do not reuse its calibration or
performance measurements as RK3576 qualification.

The user additionally classifies MaixCAM2/MaixCDK and AXMODEL platform
source, models, commands and later upstream updates as historical material
(2026-11-23), not a current development or waiting dependency. The portable
Linux detector already owned here remains current, with its historical
provenance and separately recorded verification. No AXMODEL backend is implied.

## Start here for vision teammates

The current RK3576 review source is on `codex/vision-team-review`:

```bash
git clone --branch codex/vision-team-review https://github.com/shiuhou/rm27_tracking_rk3576.git
```

Create a feature branch from this review branch for changes, and target
`codex/vision-team-review` when opening a pull request. Onboard and the pinned
combined checkout are in `shiuhou/rm27_drones` on the same review branch.

This component owns camera acquisition, payload decoding, preprocessing,
recording/replay, calibration tools, and the portable green-marker detector and
temporal tracker. In the parent workspace, `onboard/` owns process lifecycle,
configuration and the RGB adapter that connects these interfaces. PX4 transport,
flight control, mission policy and contact/recovery decisions belong to their
respective integration layers. A pixel observation is not a pose or flight command.

Read this README's build, replay and limitation sections first, then follow the
source map below. The component's requirements, ownership and milestones are in
[SPEC](.agents/docs/SPEC.md), [DESIGN](.agents/docs/DESIGN.md) and
[ROADMAP](.agents/docs/ROADMAP.md). These files describe the component contract;
local parent recordings and reports provide evidence for particular runs.

| Question | Start with |
| --- | --- |
| How does a frame move through acquisition and processing? | `src/rm27_tracking/pipeline.py`, `camera.py`, `formats.py`, `runtime.py` |
| How are original payloads and their timestamps preserved? | `src/rm27_tracking/recording.py`, `replay.py`; `tests/test_recording.py` |
| Where are candidates generated and accepted/rejected? | `detector/main/include/dart/green_detector.hpp`, `detector/main/src/green_detector.cpp` |
| Where are temporal prediction, association and confirmation implemented? | `detector/main/src/green_detector_core.cpp`; `detector/tests/test_center_kalman.cpp`, `test_predictive_center.cpp` |
| What is serialized for integration? | `detector/main/src/target_fusion.cpp`, `target_json.cpp`, `detector/tools/video_replay/rgb_stream.cpp` |
| How do we reproduce an offline comparison? | `detector/tools/video_replay/compare_recording.py`, `detector/tests/test_rgb_stream.py`, `test_compare_recording.py` |

A standalone source snapshot needs `src/`, `configs/`, `scripts/`, `tests/`,
`detector/main/`, `detector/config/`, `detector/tests/`,
`detector/tools/video_replay/`, `pyproject.toml`, this README, `AGENTS.md`,
`.agents/docs/` and `.gitignore`. Build directories, virtual environments,
generated results, raw recordings and the parent workspace are not needed for
the synthetic host tests below. Shared real-camera fixtures are local payloads,
not bundled clone inputs; obtain their catalog and original files before trying
to reproduce the real-input results. Review the provenance and publication
sections before distributing imported source.

## Linux green-marker detector and tracker

The C++ implementation and RGB adapters live in [`detector/`](detector/).
Onboard assembles these with the Python camera and recording interfaces using
its existing RGB pipe. Build from this component's root with CMake >=3.13, a
C++17 compiler and OpenCV 4 development libraries (`core`, `imgproc`, `videoio`).
The Python `opencv-python` wheel supplies `cv2`, not the C++ headers and CMake
package. On Debian/Ubuntu the native dependencies are commonly supplied by
`build-essential`, `cmake` and `libopencv-dev`; use the selected distribution's
packages. Install the Python dependencies from the next section before running
the Python checks. Use the same CMake entry point on ARM64, with native or matched
cross-toolchain dependencies; a host build does not qualify RK3576 execution.

```bash
cmake -S detector/tools/video_replay -B detector/build/video-replay -DCMAKE_BUILD_TYPE=Release
cmake --build detector/build/video-replay -j2
cmake -S detector/tests -B detector/build/tests
cmake --build detector/build/tests -j2
ctest --test-dir detector/build/tests --output-on-failure
python detector/tests/test_rgb_stream.py
PYTHONPATH=src python detector/tests/test_compare_recording.py
```

Keep assertions enabled in the native test build (the command above uses the
default build type). The optional historical `AX_SDK_INCLUDE` test requires
Maix/AX-specific sources outside this extracted Linux tree and is not part of
the supported RK3576 build. Leave that option unset; no MaixCDK, AX SDK,
AXMODEL file, NPU runtime or physical camera is required for these host checks.

For a bounded, encoded-video detector replay, create a new output directory and
pass the configuration explicitly from the component root:

```bash
mkdir -p artifacts/detector-replay-001
detector/build/video-replay/dart_video_replay \
  --input /absolute/path/to/video.mp4 --config detector/config/green_detector.conf \
  --native-resolution --max-frames 120 --no-overlay-video \
  --jsonl artifacts/detector-replay-001/targets.jsonl
```

This runs the classical detector; Python replay uses a generic channel-statistics
processor by default. Match the configuration to the actual input before evaluating detection;
`simulation_green.conf` is a generated-fixture profile, not a real-camera tuning
result. Encoded-video replay does not recover sensor/exposure timing.

`dart_rgb_stream` accepts a bounded JSON header followed by contiguous RGB3 bytes.
The `rm27-rgb-v1` protocol carries simulation time; `rm27-camera-rgb-v1` carries
host camera receipt time. Dimensions must match the configuration, sequence and
time increase within an epoch, and a new epoch resets the detector. The adapter
disables NPU and pose output and emits `safe_for_control=false`, invalid pose and
invalid angles. Camera receipt time is not exposure time. Its generated-image
protocol tests exercise the compiled binary, including default/diagnostic parity.

The portable build baseline was imported byte-for-byte from `onboard-b5f24de264ad6e8980579e2e`
(`source-inputs.tar.gz`, SHA-256 `7f35da0701ea842cd60aa810400edc66aa75d65031210152f14124c76d376534`).
The detector files originate from `blademaster679/maixcam2_dart_vision` through the historical
`m3c_tracking` checkout. The simulation profile and native regression inputs come
from `85a9182a0d9f6ab5dd20c7556a6dccd095deec8d`. The historical checkout and P1
archive retain the original source paths for reproduction. The archive hash is
inherited import evidence; it was not rechecked during the teammate delivery
build. The historical local checkout is
`rm27/perception/vision/rm27_tracking_m3c/` in the parent workspace (formerly
`maixcam2_dart_vision`); its origin is
`https://github.com/blademaster679/maixcam2_dart_vision.git`.

The inspected upstream `ac83e8c224214bc99483f9617450e110c5c16022` and historical
`85a9182a0d9f6ab5dd20c7556a6dccd095deec8d` trees have no identified
LICENSE/COPYING/NOTICE file or explicit grant in the inspected source/readme
headers. Redistribution terms remain unresolved; a public repository alone does
not identify a license. No replacement license is assigned here. Preserve the
origin, revisions and file provenance when reviewing terms for distribution.

### Offline stage diagnostics

`dart_rgb_stream --config CONFIG --diagnostics` adds a versioned
`stage_diagnostics` object to each reply. Omitting the flag retains the default
output schema. It observes the existing computation without changing detection
thresholds or decisions. Search rectangles and sample centers use source pixels.
Counters distinguish sparse proposals, exact checks, NMS, appearance rejection,
retained candidates, score/cone gating and tracker confirmation. Selected candidates
can still fail tracker association; selection alone is not a fresh measurement.

At most 128 exact-passed peak outcomes are sampled per frame. `reason_counts`
counts every visited outcome, `samples_dropped` reports sample truncation, and
`peaks_unvisited` reports exact-passed peaks skipped by the existing retained-peak
or candidate budgets. Sparse proposal budgets have separate before/after counters.
Samples describe only normalized multiscale peaks; legacy LAB candidates have
no peak samples. Scheduler coast clears per-frame diagnostics even when internal
candidate caches remain populated. No annotated boxes enter detection.

The reusable offline comparison runs baseline, current default and current
diagnostic binaries once each. It hashes inputs before replay, preserves original
capture receipt timestamps, checks full non-timing output parity, and reports
original kernel gaps separately from replay drops. It makes no speed claim.

```bash
PYTHONPATH=src python detector/tools/video_replay/compare_recording.py \
  --baseline /path/to/baseline/dart_rgb_stream \
  --candidate /path/to/current/dart_rgb_stream --config /path/to/capture.conf \
  --recording /path/to/recording --out artifacts/stage-comparison \
  --annotations /path/to/reviewed.csv \
  --supplemental-frame 1261 --supplemental-frame 1262 --supplemental-frame 1263
python detector/tests/test_rgb_stream.py --binary /path/to/current/dart_rgb_stream
python detector/tests/test_compare_recording.py
```

Annotations are optional human XYWH boxes, evaluated after replay with fresh
confirmed raw pixel centers inside the box. Supplemental detector-selected frames
are reported separately. Missing or truncated samples cannot establish a local
rejection cause. Search rectangle coverage alone does not prove cone-mask coverage.
Recordings and reviewed labels are local evidence, not bundled test fixtures.

Verified on 2026-11-23 against the unchanged RK3576 USB 1280×720 MJPG capture
`moving-capture-cbobEf`: baseline/default/diagnostics each replayed all 1,281 frames
with identical RGB and non-timing measurement outputs, 491 original source gaps
and zero new replay drops. No diagnostic samples were truncated. All 16 uniform
visible human boxes still lacked a fresh confirmed observation; their 1,560
exact-passed peak centers were appearance-rejected (1,388 color fraction,
74 component chroma, 69 radial contrast, 29 component boundary).
The three detector-selected supplemental positives all matched, with raw-center
error 0.981–1.725 px from box center; they do not change uniform recall.
This reproduces the earlier temporary candidate trace through the reusable
interface. It establishes the software rejection stage, not a physical exposure,
lighting or white-balance cause. No thresholds changed, and the new binary has
not been deployed or timed on RK3576. The parent workspace retains the detailed
comparison at `components/rm27_tracking_rk3576/.agents/runs/tracking-stage-diagnostics/replay/comparison.json`.

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

The default platform probe reads versions/build flags without device access.
Explicit `--device`/`--media` arguments enable read-only capability queries (below).
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
  `preprocess`. `Frame` includes stream/source sequence and optional original receipt,
  replay receipt and acquisition metadata; existing callers retain their defaults.
- `camera.py` provides direct V4L2 MMAP capture through single-planar or
  one-memory-plane multiplanar APIs; `formats.py`
  converts negotiated payloads to owned uint8 BGR arrays.
- `runtime.py::run_pipeline` is shared by replay, camera and benchmarks.
  `processor` is a callable receiving the processed image. The default generic
  processor reports channel mean/stddev using OpenCV, with no robotics semantics.
- `replay.py` retains `rm27-replay`; `capture.py` adds explicit `rm27-vision`;
  `benchmark.py` provides `rm27-benchmark` through the same decoders.
- `recording.py` owns the versioned raw-payload format; `calibrate.py` provides
  `rm27-calibrate` using OpenCV checkerboard detection and pinhole calibration.

`preprocess()` returns pixels independent of the input by default. An OpenCV
transformation already owns its output, so transformed paths avoid a redundant
final copy. An explicit `borrow_input=True` opts into a read-only view for an
identity operation; the caller must retain and avoid mutating the backing image
for the view's lifetime. File decoder outputs are already owned. MMAP data is
converted/copied before QBUF returns a buffer to the driver.
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

Ordinary image/video replay timestamps remain `round(sequence * 1e9 / configured_fps)`, domain
`replay`, semantic `synthetic_sequence_fps`. Container PTS, variable-rate timing
and exposure timestamps are not inferred. `received_monotonic_ns` records host
receipt separately. `configs/replay.json` contains processing choices, not camera
mode claims: width/height together resize (aspect ratio can change), `grayscale`
converts BGR to gray, and optional `max_frames` bounds the run. Unknown keys fail.

## Linux camera and RK3576 configuration

No physical camera was opened during S2, S3 or S3.5. After device identification, edit a
copy of `configs/rk3576.example.json`, then run explicitly on the camera host:

```bash
rm27-vision --config configs/rk3576.example.json --out artifacts/camera-001
```

This nested JSON has `camera`, `processing`, `runtime` and optional `recording` sections. Device path,
width/height, FPS and FOURCC are configurable. Optional camera mode values are
null by default because no sensor/mode has been verified. `processing.fps` is a
replay clock setting; it does not command a camera (use `camera.fps`). Requests
may be adjusted by drivers; `camera_requested` and `camera_reported` distinguish
requested parameters from negotiated width, height, stride, format, FPS,
colorimetry, driver and kernel buffer count. Rejected/unsupported formats fail.
`configs/camera.example.json` remains a valid camera-only Python API config;
the live CLI uses the nested S2 file. `CameraConfig`/`CameraSource` remain
importable from `pipeline` for S1 callers.

Supported progressive linear formats: **NV12, NV21, YUYV, UYVY,
BGR3, RGB3, GREY, MJPG**. Padded row strides are respected. YUV conversion uses
OpenCV BT.601 limited range; driver-declared other/unknown colorimetry is rejected
rather than misconverted. `camera.capture_type` is `auto` (prefer single-planar
if advertised, otherwise multiplanar), `single` or `mplane`. The multiplanar API
currently requires exactly **one memory plane**, including contiguous NV12/NV21;
negotiated type/plane count and per-frame length/bytesused/data_offset are retained.
Raw recording keeps the prefix through `bytesused`; decoding begins at
`data_offset`. Separate-memory-plane formats such as NM12/NM21, tiled layouts,
interlaced frames and ISP/media-graph configuration remain unsupported. This
host-tested adapter still needs qualification against the selected BSP.

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
in `finally` when they stop early. Sources are single-use. Standard exposure-auto,
absolute-exposure, priority and legacy exposure controls are queried read-only once
per open. Their availability, bounds, flags, values, units, query times and provenance
are recorded. Absolute exposure uses the UAPI unit of 100 microseconds; legacy
exposure units stay driver-defined/unverified. Readbacks are not frame-synchronized
exposure measurements. No exposure control is changed. Triggering, hardware sync,
gain control and automatic camera probing are not implemented.

## Original-payload recording and exact replay

After identifying a supported device/mode, opt in using
`configs/recording.example.json` (10-second run, 1 GiB payload limit; modes remain
null until selected). Recording requires **both** a positive `recording.max_bytes`
and a duration/frame cap. Example commands, to run on the selected camera host:

```bash
rm27-vision --config configs/recording.example.json --out artifacts/capture-001
rm27-replay artifacts/capture-001/recording/manifest.json --out artifacts/exact-replay-001
# Explicit recovery of an interrupted recording's verified complete prefix:
rm27-replay artifacts/interrupted/recording --allow-incomplete --out artifacts/recovered-001
```

`recording/manifest.json` identifies schema version 1 and completion status;
`payload.bin` is append-only exact `bytesused` data; `frames.jsonl` indexes offsets,
lengths and SHA256 along with negotiated FOURCC/dimensions/stride/sizeimage/
colorimetry, driver flags, sequence, stream epoch and source/host timestamps.
Read-only exposure snapshots are retained in the negotiated-format metadata.
Records are written synchronously **before QBUF and before application queue
eviction**, with no second writer queue. A recorded frame may therefore have no
processed result. A successful raw append reserves its application sequence even
if QBUF then fails. Empty restart epochs and legitimate source gaps are counted;
duplicate/out-of-order records and backward epochs are rejected.

The byte limit bounds payload bytes, excluding JSON metadata. The next whole
frame is declined before writing if it would exceed the limit; stop reason is
`recording_byte_limit`. No existing output directory is overwritten. Storage or
finalization failures leave the recording incomplete and fail the run; complete
index entries and partial tail evidence are retained. Writes returning do not
promise disk durability. Per-payload write time and bounded P50/P95/P99 full
append overhead are reported. A host-only probe writing 128 synthetic 640×480
NV12 payloads (56.25 MiB) measured mean 0.463 ms and P50/P95/P99
0.453/0.609/0.638 ms per append, and all 128 replayed exactly. This measured
application writes through the host page cache, with no fsync or camera; it is
not board/storage durability throughput. Slow storage can cause real driver drops; host
fixtures cannot measure that effect on a camera.

Replay checks every selected record's bounds, hash, order, epoch, format and
colorimetry using the existing converter. Complete recordings also require exact
final counts/file sizes. Incomplete recordings need `--allow-incomplete`; only a
verified complete prefix is delivered, and corruption still fails. Selected
`max_frames` prefixes do not certify unread tails. Replay holds one payload at a
time and releases it before yielding owned BGR pixels. Index/manifest records
are capped at 1 MiB; `RecordingSource` defaults to a 128 MiB per-frame raw/declared
BGR allocation guard (Python API override for larger future modes). This is not
an adversarial compressed-media decoder sandbox.

Recorded `timestamp_ns`, `received_monotonic_ns` and all acquisition metadata
remain original. `original_received_monotonic_ns` explicitly names the original
receipt; `replay_received_monotonic_ns` is freshly measured on the replay host.
Current performance intervals use the fresh receipt, including when the original
host clock epoch is numerically greater than today's host clock.

## Checkerboard calibration estimates

Provide native-resolution checkerboard images, a recording directory/manifest,
or video. Declare the **inner** corner columns/rows and physically measured square
side/unit; there are no assumed board dimensions. Set the following shell values
from your board measurement and experiment's acceptable evaluation error:

```bash
rm27-calibrate /absolute/path/to/checkerboard-images --out artifacts/calibration-001 \
  --inner-columns "$INNER_COLUMNS" --inner-rows "$INNER_ROWS" \
  --square-size "$MEASURED_SQUARE_SIDE" --square-unit "$SQUARE_UNIT" \
  --max-evaluation-error-px "$MAX_EVALUATION_ERROR_PX" \
  --sample-step 1 --max-candidates 300 --max-views 60
```

Work is deterministic and bounded: at most `max_candidates * sample_step` decoded
frames, detection every `sample_step`, at most `max_views` distinct observations.
Mixed native image sizes fail. Default minimum is 15 accepted views (12 fit,
3 evaluation); every fifth accepted view is reserved for evaluation before fitting.
An explicit `--min-views` can adjust this, with a floor of 10. Distinctness uses
pixel hashes and corner displacement (default 1 px); frontoparallel/degenerate
view collections are rejected by the planar-constraint conditioning check.
Acquire varied board poses and image coverage; these numerical checks cannot
validate a physical board, lens or installation.

OpenCV fits a standard pinhole model with exactly `k1,k2,p1,p2,k3`. Held-out board
poses are solved using frozen intrinsics; they never enter intrinsic fitting.
`quality.json` reports accepted/rejected views and reasons, image coverage,
overall/per-view fitting error and held-out RMS errors. Export requires **every**
held-out view to meet your declared error threshold. Insufficient, degenerate,
nonfinite or threshold-failing solutions yield no `calibration.json`; poor detected
views are not silently removed to obtain a pass. Successful exports include board
measurement declarations, source/pixel/payload hashes, image size, coefficients
and quality, labeled **calibration estimates requiring physical validation**.
No automatic pipeline undistortion is introduced. Generated checkerboard tests
validate software only. Real calibration is **BLOCKED** pending suitable real
images and a measured physical target.

Load an exported estimate while enforcing its native resolution:

```python
from rm27_tracking.calibrate import load_calibration
estimate = load_calibration("artifacts/calibration-001/calibration.json",
                            image_size=(native_width, native_height))
```

The loader rejects invalid/nonfinite matrices, unsupported distortion models,
inconsistent quality gates and resolution mismatches. It does not rescale or
undistort. Match the physical camera, lens, focus, sensor/ISP crop and resolution
to the calibration session. An existing OS04A10 circle-grid calibration from
another component/session is not a calibration for the selected forward camera.

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

Runs write `summary.json`, sampled `frames.jsonl`, separate `publications.jsonl`,
`resources.jsonl` and the first
processed `preview.png`. Replay additionally writes ordered `inputs.jsonl`
paths/hashes. Keep source files immutable during a run. Frame logging frequency
and processed-pixel hashing are configurable; frame logs grow with the chosen
sample rate, resources are sampled at most once per second. No per-frame images
are retained. Runtime statistics use constant memory for arbitrary run length.

Summary metrics include acquisition throughput/latency, preprocessing and generic
processor throughput, stage means/max/P50/P95/P99, queue wait/depth/drops, host residence,
CPU time/utilization and RSS samples. Acquisition duration includes source decode
or MMAP format conversion; in a paced benchmark it also includes deliberate wait.
Processing FPS is compute-only reciprocal of mean preprocess+processor time.
Wall throughput includes pipeline I/O and instrumentation. CPU utilization is
process CPU / wall time ×100; more than 100% means more than one CPU core's time.
Linux `/proc/self/statm` supplies sampled RSS; process-lifetime `ru_maxrss` is
reported separately. P50/P95/P99 use an all-sample log histogram with 1.03 bucket ratio
(upper-edge approximation error <=3% of latency plus 1 µs). Host residence starts
at the current host receipt and ends after the selected output operation; it is
**not sensor-to-result latency**. Each logged result records host-monotonic
preprocess/processor start/end and output start. A separate publication event is
written after its frame JSONL flush (and the first preview write) has returned.
Publication is application operation completion, not disk durability, and applies
only to selected outputs. `host_to_processing_complete` covers all frames. No
stage compares unrelated driver and host clock domains; camera-to-output remains
unavailable without a verified clock relationship. With hashing enabled,
`processed_frames_sha256` covers every processed frame in order regardless of log
sampling, including shape/dtype and pixels; source times are excluded.

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

S3 real-input host runs on 2026-10-31 (Asia/Shanghai) used the existing read-only
`rm27_drones/rm27/perception/vision/video/0921.mp4`: 230,317,363 bytes,
SHA256 `437d72cb0030cdb33d1ea8377b2d61e234280813dbb83c85ab3c8e3e44fe50bb`.
Hash was computed before analysis and rechecked unchanged afterwards. Decoded
size is 1344×760; 13,766 frames were decoded in each full pass, matching the
reported count; nominal 180 FPS is an encoded-stream property. Parent reference
`rm27/perception/vision/docs/VSL-1A_0921.md` attributes this hash to a user-provided
M3C/OS04A10 recording. This supports historical attribution, not verified sensor,
exposure, synchronization or sensor-drop provenance.

Reproduce the real-input runs (each output directory must be new):

```bash
VIDEO=/absolute/path/to/0921.mp4
rm27-benchmark --input "$VIDEO" --fps 180 --complete --out artifacts/s3-real-pass1
rm27-benchmark --input "$VIDEO" --fps 180 --complete --out artifacts/s3-real-pass2
rm27-benchmark --input "$VIDEO" --fps 180 --seconds 60 --paced --out artifacts/s3-real-paced
```

These **sequential**, separate processes used x86_64 Linux/Python 3.12.3,
OpenCV 5.0.0/NumPy 2.5.1/rm27_tracking 0.3.0, one OpenCV thread, queue capacity 2
with `block`, resize to 320×240 grayscale and generic mean/stddev, hashing every
processed output and logging every 60th. Decoder-internal threading is not limited
by `opencv_threads`; CPU can exceed one core. Source hashes, full configurations,
versions and exact implementation-file hashes are in each benchmark summary.

| Real encoded-video run | Processed | Wall FPS | Compute FPS | Preprocess+processor P50 / P95 / P99 ms | CPU | Max RSS MiB | Queue drops / high water |
| --- | ---: | ---: | ---: | --- | ---: | ---: | --- |
| Full pass 1, 34.82 s | 13,766 | 395.32 | 415.42 | 2.448 / 2.522 / 2.838 | 242.33% | 134.09 | 0 / 2 |
| Full pass 2, 35.41 s | 13,766 | 388.80 | 409.33 | 2.448 / 2.597 / 3.815 | 246.92% | 134.72 | 0 / 2 |
| Paced, 60.00 s | 10,797 | 179.94 | 407.62 | 2.448 / 2.675 / 4.047 | 113.32% | 131.08 | 0 / 1 |

Unpaced acquisition/decode-call means were 0.521 and 0.687 ms (P95 1.240 and
1.990 ms); their reciprocals describe source-call work, not achieved end-to-end
throughput. Paced acquisition includes intentional waiting (mean 5.539 ms).
The full passes have identical ordered processed-pixel digest
`d5c5d4f27d5fe3b46879954653408cb64d478b95e2bee1d15fad8575c1d7fde0`.
All 180 sampled paced-output hashes match the corresponding full-pass prefix;
its shorter duration-selected sequence has a different aggregate digest, as
expected. There were no application sequence gaps. These observations cannot
recover original sensor drops or exposure latency from encoded video.

Evidence is `artifacts/s3-real-{pass1,pass2,paced}/run/summary.json` and adjacent
sampled frame/publication/resource logs. Every stage includes P50/P95/P99.
Paced sampled RSS grew 10.36 MiB from decoder initialization; after 10 seconds
it stayed 130.59–131.08 MiB, ending 0.49 MiB above that interval's first sample.
This 60-second observation does not establish long-term leak freedom or RK3576
resource behavior. The old S2 synthetic runs above have different input and
instrumentation and are not a performance comparison.

## ARM64 installation and verified scope

**ARM64 EMULATED: PASS.** `scripts/verify_arm64.py` builds the portable 0.3.0
wheel, installs it outside the checkout in pinned AArch64 Python userspace and
runs tests, all four CLIs, deterministic video replay/benchmark and missing-camera
failure under QEMU. Python, NumPy and OpenCV ELF machine types and loaded shared
libraries are recorded. There are no privileged containers, host binfmt changes
or camera mounts. This verifies ARM64 software execution, not RK3576 performance,
camera access or a vendor BSP.

Final emulated suite: **61 run, 60 passed, 1 compiler check skipped**, with one
native-speed timeout test excluded and separately checked as described below.

Reproduce on Linux x86_64 with Docker, `dpkg-deb`, a host C compiler (`cc`) with
static libc, and Python with pip/setuptools/wheel; internet is needed for pinned
dependencies. Clang enables the additional
target ABI compilation; its absence is reported as NOT_RUN.

```bash
python scripts/verify_arm64.py --out artifacts/arm64-check
```

Verified environment: QEMU 8.2.2, Debian 12 AArch64, Python 3.12.3,
NumPy 2.2.6, OpenCV headless 4.11.0. The script pins the container digest and
SHA256 values for the emulator, ARM64 wheels and target headers. Evidence under
`artifacts/s35-arm64-final/` includes exact wheel/source hashes and shared-library
paths. The wheel bundles codec/OpenBLAS libraries; glibc, libstdc++, libgcc_s,
libm and zlib come from the container and still need verification on the BSP.
The C header test is skipped in the compiler-free guest; its sizes, alignments,
field offsets and ioctl values are instead **CROSS-COMPILED: PASS** against
AArch64 Linux 6.8 UAPI headers with Clang 18.1.3 (AArch64 ELF object, no device
access). A tiny static host bridge starts ARM64 Python subprocesses through
explicit QEMU, including the original replay CLI test. The platform test's
0.2-second timeout assumes native startup speed and is excluded under emulation;
the same ARM64 timeout/kill/reap path is checked separately with a declared
2-second allowance. Original host tests and application defaults are unchanged.

Retain native installation for an Ubuntu/Debian-like ARM64 BSP. The following
commands require board access and BSP-supported package versions; they have
**not** been run on RK3576:

```bash
sudo apt install python3-venv python3-opencv python3-numpy python3-setuptools python3-wheel build-essential linux-libc-dev v4l-utils
python3 -m venv --system-site-packages .venv
. .venv/bin/activate
python scripts/check_platform.py
python -m pip install --no-deps --no-build-isolation .
python -m unittest discover -s tests -v
python -m pip wheel --no-deps --no-build-isolation . --wheel-dir dist
rm27-benchmark --seconds 60 --paced --queue-policy latest --out artifacts/arm64-replay-001
```

The native test suite compiles a probe against **that host's** Linux headers;
rerun it on the board to check its actual ABI. The pure-Python wheel has no
project C++ runtime or camera SDK dependency. Distro/BSP OpenCV remains supported;
do not replace its ISP/codecs with a pip distribution without checking the BSP.

## RK3576 camera evidence and first boot

The forward camera sensor is **OS04A10**, confirmed by the user with Sipeed's
camera page. The downward camera is undecided. The parent repository's supplied
`RK3576/` bundle identifies **Beijing HZHY / 北京合众恒跃**, HZ-CORE-RK3576 and
HZ-EVM-RK3576. Following the parent's `docs/REAL_HARDWARE.md` DDR5-only reference
selection, use `01_硬件手册/硬件手册_新版（LPDDR5_CV00)/` for board-specific work:
**LPDDR5 CV00 core / MV12 carrier**. This selects the documentation; the purchased
core revision, carrier and installed BSP still need confirmation. No physical
`/dev/video*` or media node is available on this host. Local source paths below
are relative to that `RK3576/` bundle.

The bundle's `01_硬件手册/README (用户设计底板前先看此说明）.txt`
(dated 2026-01-27; SHA256
`90192e5bbb9984d15c92aee812dbb5c319c1c9e7a537596361d5421b89c7e26e`)
maps **LPDDR4 CV02 → MV01** and **LPDDR5 CV00 → MV12**. It explicitly warns
that their connector pin ordering differs and the evaluation boards are not
interchangeable. Do not substitute LPDDR4 pin tables, schematics or specifications
for the selected DDR5 references. Check the actual core/carrier markings before
applying a pin table; RAM/storage capacity or the same BTB connector shape does
not identify the revision. Keep vendor files read-only; this independent package
does not redistribute the bundle. Standalone clones need these references
separately before board-specific configuration.

`02_镜像及源码/01_HZ-EVM-RK3576_LINUX_SDK/README.md` contains only an instruction
to obtain materials through the vendor's business/FAE contact. No SDK, rootfs
image, kernel source/config, DTS/DTB or target sysroot is present in the supplied
bundle. The Linux environment guide V1.0, pp.45–47, recommends an Ubuntu 20.04
**build host**, but includes Linux 5.10/4.19 and RK3588 toolchain examples; those
examples do not establish the RK3576 target kernel, OS or ABI. The native Debian/
Ubuntu installation commands above remain conditional on the actual rootfs and
Python/OpenCV versions, rather than a verified HZHY image recipe.

| Primary evidence | What it establishes / remaining boundary |
| --- | --- |
| Local HZHY `01_硬件手册/硬件手册_新版（LPDDR5_CV00)/01_产品规格书/HZ-EVM-RK3576-规格书V1.1.pdf`, pp.9–12; MV12 schematic, pp.11–12 | Reference EVM has three four-lane camera connectors and camera power/clock/reset/I2C-switch circuitry. This is not an OS04A10 flex/adapter wiring definition. |
| Local `03_文档教程/1、开发板使用手册/HZ-EVM-RK3576-测试手册V1.1.0(1).pdf`, pp.21–23 | Vendor camera examples use IMX415 (up to 4K30) and GC05A2; J13/J14/J15 map to `rkisp_mainpath` examples `/dev/video33`, `/dev/video42`, `/dev/video51`. A GStreamer preview requests NV12 1920×1080 at 30 FPS. These example nodes/modes are not stable device identities or OS04A10 support evidence. |
| Sipeed camera comparison [1] | OS04A10 is a rolling-shutter sensor, listed at 2688×1520 with 2.9 µm pixels. Sipeed's advertised modes/lens apply to its module/MaixCAM integration; they do not qualify the purchased lens or RK3576 connection. |
| Rockchip RK3576 product page [2] | Linux SDK, MIPI CSI and ISP exist at SoC level. Carrier lane wiring, connectors and supported camera modes still require vendor files. |
| Rockchip kernel `77168c8d…`, `drivers/media/i2c/os04a10.c` [3] | A driver exists with `ovti,os04a10`, endpoint-selected two/four-lane modes, RAW10, 24 MHz xvclk, supply/reset resources and module/lens metadata. This is reference implementation evidence, not proof that the selected BSP includes it or that a module is electrically compatible. |
| Same kernel, ISP `capture.c` and `capture_v32.c` [3] | The ISP exposes VIDEO_CAPTURE_MPLANE; contiguous NV12 uses one memory plane and NV12M uses two. This justifies the tested one-memory-plane adapter. The actual board's node, stride and format remain unverified. |

Within the selected references, the LPDDR5 core specification V1.0, p.11, says
2+2-lane splitting is temporarily unsupported, while the hardware
design guide V1.0, p.42, describes it at SoC level. Do not infer an operational
split-lane or dual-camera mode from these mixed references. The bundled ISP
guides/link index cover other SoCs and do not establish an RK3576-specific ISP
revision or matching OS04A10 IQ package. The vendor's GStreamer example also
does not verify Python OpenCV's GStreamer backend. No software format/backend
change is justified until the selected BSP's actual capabilities are available.

The pinned reference kernel is not a required BSP version. Obtain the vendor's
matching kernel/config, carrier schematic/pinout, sensor driver/device tree and
ISP/IQ tuning files. Verify power rails, reset/power-down, I2C address, xvclk,
endpoint/data lanes and CSI routing **before connecting** the module. Do not
derive pin assignments or a device tree from the sensor name. Exposure control
register values and driver timestamp flags alone do not establish physical
exposure timing, rolling-shutter readout or cross-camera synchronization.

After the vendor media graph is configured and native replay tests pass, collect
read-only evidence from the identified nodes. First list devices, then replace
the example nodes with the matching `rkisp_mainpath` and media graph; do not
assume `/dev/video0` or blindly use the vendor's `/dev/video33` example. Repeat
`--device` for the eventual second camera:

```bash
v4l2-ctl --list-devices
python scripts/check_platform.py --device /dev/video0 --media /dev/media0 --out artifacts/board-probe-001
# If exposure controls live on an identified sensor subdevice:
v4l2-ctl -d /dev/v4l-subdev0 --list-ctrls-menus
```

The collector records platform/kernel/dependency versions, capability/format/FPS/
control queries and media topology. Queries have a 10-second deadline and retain
at most 64 KiB each with explicit truncation; failures remain FAIL/BLOCKED. It
does not set controls/formats or start streaming. Required UAPI evidence:
`VIDIOC_QUERYCAP` (device_caps and
VIDEO_CAPTURE vs VIDEO_CAPTURE_MPLANE/STREAMING), `VIDIOC_ENUM_FMT` for each
advertised type, `VIDIOC_ENUM_FRAMESIZES`/`VIDIOC_ENUM_FRAMEINTERVALS`, and
`VIDIOC_G_FMT` negotiated width/height/FOURCC/field/colorimetry. For multiplanar,
record num_planes and every plane's bytesperline/sizeimage, plus DQBUF
bytesused/data_offset and flags. Record G_PARM and QUERYCTRL/G_CTRL or
QUERY_EXT_CTRL/G_EXT_CTRLS read-only exposure availability/units. These are queries
to perform on the selected board; none were executed against physical hardware.

Copy `configs/recording.example.json` to an ignored artifact path and set the
verified device/mode; choose `capture_type` from the node capabilities. Keep the
10-second/1 GiB limits for a first recording, then use the same replay pipeline:

```bash
cp configs/recording.example.json artifacts/board-probe-001/camera.json
# Edit camera.json using the negotiated device, mode and supported FOURCC.
rm27-vision --config artifacts/board-probe-001/camera.json --out artifacts/board-capture-001
rm27-replay artifacts/board-capture-001/recording/manifest.json --out artifacts/board-replay-001
rm27-benchmark --input artifacts/board-capture-001/recording/manifest.json --complete --out artifacts/board-bench-001
```

Inspect hashes, epochs/sequences, clock flags, negotiated layout, drops and host
intervals before making capture-timing claims. Next collect varied native-resolution
checkerboard views with measured board dimensions and run the calibration command
above. Finally test disconnect/restart and sustained capture under representative
load, retaining CPU/RSS/queue/thermal observations and board/driver versions.

RKNN preparation is deferred: there are no models/inference implementations in
this repository to adapt. A future optional backend needs a chosen model,
pre/postprocess contract and matched RKNN conversion/runtime versions. The CPU
pipeline remains independent of RKNN, and there are no invented NPU measurements.

[1]: https://wiki.sipeed.com/hardware/en/maixcam/cameras.html
[2]: https://www.rock-chips.com/a/en/products/RK35_Series/2024/1212/2033.html
[3]: https://github.com/rockchip-linux/kernel/tree/77168c8d5ab82399f65a80e9f807b50ba37cf483

## Historical S1/S2 code review and reuse decision

The table below records the original Python-only S1/S2 decision. The later Linux
detector import described above supersedes its no-extraction decision for the
detector, tracker, compatibility shim and RGB replay sources; the table is not a
current source inventory.

That review inspected parent `rm27_drones` and the third-party submodule then at
`rm27/perception/vision/maixcam2_dart_vision` (now the historical
`rm27/perception/vision/rm27_tracking_m3c`) at
`ac83e8c224214bc99483f9617450e110c5c16022`. Its origin remains
`https://github.com/blademaster679/maixcam2_dart_vision.git`.
Paths below are relative to that third-party repository unless noted.

| Existing component | Reuse assessment |
| --- | --- |
| `main/src/nv21_pipeline.cpp` | Stride/ROI checks and YUV sampling are generic, but output and callers use Maix/target types. Existing `formats.py` and `preprocess` already cover the required layout/resize behavior. |
| `main/src/visual_motion.cpp` | Gray downsampling, feature selection/tracking and similarity RANSAC are portable in isolation; the update path consumes Maix images and target motion priors. No current generic feature consumer requires a port. |
| `main/src/green_detector_core.cpp`, `main/include/dart/center_kalman.hpp` | Coordinate/distortion math and a small filter are separable, but association uses target schemas. OpenCV calibration/geometry already supplies the needed primitives; no target tracker is imported. |
| `tools/video_replay/maix_image_opencv.cpp` | Uses OpenCV image operations behind a Maix compatibility layer, with blob/target-specific behavior. Reuse OpenCV primitives directly; do not copy the compatibility implementation. |
| `tools/video_replay/video_replay.cpp`, `CMakeLists.txt` | Existing host replay links detector, target fusion and motion code. Reuse the file-decoding approach without importing these dependencies. |
| `main/include/dart/async_frame.hpp` | Generic owned-frame metadata, latest-frame slot and timestamp scheduler are future C++ extraction candidates. The S2 Python runtime supplies its own small bounded producer queue; no C++ port is needed. |
| `tools/os04a10_highfps/frame_continuity.hpp` | Portable sequence-gap/PTS checks; the existing child validator already covers gaps, uint32 wrap and restart epochs. Application replay indices cannot stand in for sensor counters. |
| `tools/os04a10_highfps/vin_nv21_frame.hpp` | AX frame/MMAP/release ownership is hardware-specific. Reuse the lifetime discipline, retaining this repository's V4L2 ownership implementation. |
| `main/src/config.cpp` | Parser depends on detector settings. Standard-library JSON replaces that dependency for generic configuration. |
| Parent `rm27/perception/vision/localization/manifest.py` | Explicit clock semantics, provenance and exposure-verification rules are reusable design conventions. Do not relocate or change localization infrastructure for this task. |
| Parent `rm27/perception/vision/localization/analyze_video.py` | Uses OpenCV, NumPy, hashes and offline diagnostic outputs. Reuse those underlying libraries; its VSL qualification schema and external-tool workflow stay with localization. |
| Existing target JSON/log evaluators | Target-specific schemas are excluded. Small JSONL frame/measurement records use the standard library. |

At that S1/S2 stage, no third-party source or historical artifacts were copied,
moved or deleted. No top-level license was found in the inspected third-party
checkout; later extraction retains the provenance review above. That baseline
reused the existing OpenCV/NumPy dependencies.
No reusable source package could be imported independently without coupling to
vendor or localization code; the new code is the small acquisition/metadata/CLI
adapter. No software license has been selected for this repository.

## Parent integration and publication state

This is an independently versioned repository with `origin` configured as
`https://github.com/shiuhou/rm27_tracking_rk3576.git` at the owner's request.
The review branch `codex/vision-team-review` includes the current Linux detector,
tracker, camera integration and diagnostics prepared from `integrate/onboard-vision`.
It is a source-review version; hardware qualification retains its existing scope.
Generated media, benchmark logs, virtual environments, build outputs and local
engineering plans/run records remain ignored.

S1/S2 baseline commit is `e8a9ac85522c9ed70cb68722c6fec8459be3e653`.
S3/S3.5 extend it with reproducible recordings, calibration and ARM64 preparation.
Read-only remote verification found published `main` at
`1e59b3631e7cdf73ddf91c10c0e25cddcb5799b4` (S3/S3.5), without the current
`detector/` tree. Published historical branch `m3c_tracking` is at
`85a9182a0d9f6ab5dd20c7556a6dccd095deec8d` and contains earlier green-detector
and host-replay source. The current RK3576 port and diagnostic interface are now
in `codex/vision-team-review`; `main` is the earlier acquisition baseline.
The local development branch name `integrate/onboard-vision` is not the team entry.
Use `git status`, `git log -1` and `git ls-remote --heads origin` to recheck this
snapshot before relying on a clone. No credentials are stored in this repository.

The component was formerly named `rm27_tracking`. Its existing child history and
published `1e59b3631e7cdf73ddf91c10c0e25cddcb5799b4` revision are preserved.
The parent's `.gitmodules` registration and mode-160000 gitlink determine which
path and revision recursive clones receive. Check those separately from the
child repository's working tree. Renaming the GitHub repository alone does not
publish local file changes.

Commit, push and parent gitlink publication each require explicit authorization.
The imported source terms remain unidentified; the review preserves their origin
and notices and does not assign a replacement license. Keep the full intended
source inventory and verify changes from a clean clone. Parent registration and
gitlink changes are a separate publication step. This package builds and runs
without the parent; a local source bundle does not update GitHub or its gitlink.

## Validation and remaining hardware requirements

Teammate delivery preparation (2026-11-26) copied the current source into a clean
directory without the parent workspace, build outputs or raw datasets. On Linux
x86_64 with Python 3.12.3, NumPy 2.5.1, Python OpenCV 5.0.0, native OpenCV 4.6.0,
G++ 13.3.0 and CMake 3.28.3: **62 Python tests, 13 native CTest tests, 9 compiled
RGB protocol tests and 9 comparison-tool tests PASS**. Both `dart_video_replay`
and `dart_rgb_stream` build successfully. A generated 8-frame MP4 replay capped
at 5 frames produces exactly 5 JSONL rows using the documented native-resolution
command. These checks use generated inputs and preserve detector thresholds and
output gates; they do not rerun the inherited
1,281-frame real recording, measure detection accuracy or establish RK3576 speed.
The comparison tool's SHA-256 reader also avoids the Python-3.11-only
`hashlib.file_digest` API so it matches the declared Python >=3.10 floor. Python
3.10 execution itself was not available in this host verification; empty,
3-byte and 2,097,408-byte files retain their SHA-256 with that newer API removed.

S1's five tests passed before edits. S2 preserves its image/video ordering,
preprocessing, failure reporting and CLI through the common runner; its obsolete
OpenCV camera mock test was replaced by production V4L2 lifecycle tests.
S3 suite: **53 tests PASS** under both OpenCV 5.0/NumPy 2.5.1 and distro
OpenCV 4.6/NumPy 1.26.4, retaining all 23 S2 tests. Added exact padded YUV/MJPEG
payload round trips, hash/bounds/truncation/order/restart verification, byte limits,
write/finalization failures, bounded recording/replay memory over 5,000 frames,
record-before-eviction/requeue failure, read-only exposure queries, fresh replay
clock epochs, publication ordering, deterministic real-video fixture passes and
checkerboard detection/fitting/evaluation/export refusal. Generated calibration
fixtures exercise software; they do not validate a real camera.

S3.5 preserves those regressions and adds one-memory-plane UAPI/layout/ownership/
cleanup tests, offset-preserving recording/replay with strict plane metadata,
calibration loading/resolution checks, and bounded read-only platform queries.
**HOST-ONLY: 62 tests PASS** on both OpenCV 4.6 and 5.0; all 62 also pass against
the wheel installed outside the checkout. The original full-video S3 results
above remain the real-input baseline and were not repeated for S3.5.
The 0.3.0 wheel still builds without new runtime dependencies and is tested after
installation outside the checkout. All four entry points, generated-video replay,
exact raw replay, deterministic benchmark and expected camera/calibration failures
are exercised. See the ARM64 section for its separate emulated-runtime and
cross-compiled ABI results; neither is physical RK3576 validation.

Remaining RK3576 requirements:

- Obtain the board/carrier/BSP details when the user supplies them; forward sensor
  OS04A10 is selected, downward camera is still undecided.
- Qualify the actual driver/media graph, electrical connection, negotiated
  progressive single-planar or one-memory-plane multiplanar mode, colorimetry,
  stride and ISP output. Separate-memory-plane support needs actual format evidence.
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


## Shared real-camera test videos

[Shared video catalog](data/shared_vision/README.md) provides four user-supplied
real drone-camera MP4 fixtures, hashes, reported capture settings, decoding checks
and replay instructions. `data/shared_vision/` is a relative link to the shared
RM27 dataset, with one physical copy; videos are local data, not included in a
standalone clone. Keep derived test outputs separate from these inputs. See the
catalog for exposure-mapping and synchronized-IMU/timestamp limitations.

## Shared M3C and MaixCAM2 board references

[Shared board catalog](data/shared_m3c_board/README.md) provides the user-supplied
M3C/MaixCAM2 schematics and 3D/mechanical files, with a SHA-256 manifest.
Use `data/shared_m3c_board/raw/` from this checkout; all local aliases share one
physical collection. Match the document to the actual board revision. Raw files
are local references supplied separately from Git clones; see the catalog for paths.

## Shared RM27 aircraft model

The current shared simulation aircraft is **ASM2 v1.0.0 (142 g)**.
For new MuJoCo work, use the **v0.1.0 collision proxy** from the
[shared aircraft catalog](data/shared_aircraft/README.md).
Existing experiments retain their pinned model and configuration.
