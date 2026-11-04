# RGB 视频离线回放

需要在 MaixCAM2 上复用实时 NV21 ROI 业务流程测速时，使用
[NV21 板端回放入口](NV21_BENCHMARK.md)；全幅 RGB 板端对照见
[RGB 板端基准](BOARD_BENCHMARK.md)。下面介绍主机 RGB 回放工具。

构建：

```bash
cmake -S tools/video_replay -B build/video-replay -DCMAKE_BUILD_TYPE=Release
cmake --build build/video-replay -j
```

默认逐帧处理、输出叠加视频及 JSONL；处理分辨率来自配置，
`--native-resolution` 使用输入视频分辨率。现有命令保持兼容。

长视频可按源视频帧号抽样，仅输出识别 JSONL：

```bash
build/video-replay/dart_video_replay \
  --input recordings/maixcam2/2026-09-20/clip_000013_test5min/record_180fps.mp4 \
  --config config/green_detector_full180.conf \
  --native-resolution --frame-step 6 --no-overlay-video \
  --jsonl artifacts/clip13/replay.jsonl
```

- `--frame-step N` 处理源帧 `0,N,2N,...`。JSONL 的 `frame` 保留源视频的
  零起始帧号，`timestamp_us=round(frame*1000000/source_fps)`。
  此时间轴来自容器标称帧率，不恢复录制时丢帧造成的真实时间间隙。
- `--max-frames N` 始终限定读取的源帧范围 `[0,N)`，不是抽样后的处理帧数。
- 保留叠加视频输出时，输出帧率为 `source_fps/frame_step`，播放速度保持不变；
  最后不足一个采样周期时，输出时长可能多出不足一个输出帧周期。
- `--no-overlay-video` 不创建视频或绘制叠加层；须提供 `--jsonl`，也可以保留
  `--output` 仅用作默认 JSONL 路径的前缀。
- 汇总的 `frames`、检测率和平均处理 FPS 均按实际处理帧计算；
  `source_frames_read` 单独给出读取的源帧数。
- 抽样会改变时序检测器接收帧的频率，确认命中数和丢失帧预算按处理帧计；
  视觉运动估计的 `interval_frames` 也按处理帧计。不同抽样间隔的结果不能直接
  当作相同采集频率下的性能。正式比较应使用同一视频、同一帧间隔及同一真值。

该工具将视频解码成 RGB，运行 `GreenLightDetector`；它不运行板端高速 NV21
ROI 转换、分阶段调度或原始帧元数据链路。逐帧 JSON 和汇总显式记录
`replay_pipeline="rgb"`、`frame_step`、`replay_timestamps="nominal_source_fps"`。
离线有效输出率不是识别准确率，也不能证明板端实时性能。

当配置请求 `camera.fps>60`（包括未经标定的 full180 配置），或关闭
`capture.enabled` 时，工具把此回放视为诊断用途，记录
`replay_diagnostic_only=true`，强制 `angles_valid=false`、
`safe_for_control=false`、`pose.valid=false`，清空视线角及相关角度量。
关闭捕获锥本身不证明相机未标定，这是此回放工具采用的保守诊断约定。
像素坐标、绿灯和双灯条识别结果仍正常输出，可用于标注比对。

独立 CLI 回归（需要 Python 的 OpenCV 和 NumPy）：

```bash
python3 tools/video_replay/test_video_replay.py \
  --binary build/video-replay/dart_video_replay
```

## Persistent simulation RGB interface

The same build creates `dart_rgb_stream`. It runs the original
`GreenLightDetector::process()` and shared `TargetEstimate` JSON v2 serializer
with this directory's existing OpenCV RGB image shim. It does not open a camera
or connect to a flight controller. The host-only profile below uses a 640×480,
10 Hz simulated camera with **vertical** FOV 70° and square pixels
(`fx=fy=342.7555216`, principal point `319.5,239.5`). Its 35° capture cone
permits the simulation's initial ±15° heading offsets. These are declared
rendering parameters, not measured OS04A10 intrinsics or hardware defaults.

```bash
build/video-replay/dart_rgb_stream \
  --config tools/video_replay/simulation_green.conf
python3 tests/test_rgb_stream.py --binary build/video-replay/dart_rgb_stream
```

Keep one child process, send one request, and read one response before sending
the next. A request is a JSON object terminated by one LF, immediately followed
by `height*stride` raw bytes, top row first, channel order RGB:

```json
{"protocol":"rm27-rgb-v1","stream_epoch":0,"source_sequence":0,"simulation_timestamp_us":0,"width":640,"height":480,"stride":1920}
```

All seven keys are required; unknown or duplicate keys are rejected. This
bounded protocol subset uses unescaped ASCII keys and protocol strings. Numeric
fields must be nonnegative decimal JSON integers. Sequence and simulation time
are bounded by `2^53-1`, epoch by `2^31-1`; dimensions are 1–4096 and must match
the selected config. Frames must be contiguous (`stride=width*3`), with payload
at most 32 MiB and header at most 4096 bytes excluding LF. There is no implicit
resize or color conversion. Within an epoch, sequence and simulation time must
strictly increase; sequence gaps are counted. A greater epoch rebuilds detector
and visual-motion state, allowing sequence/time to restart at zero. Reversed
epochs, duplicate/reversed sequence/time, malformed/truncated requests and
write failures terminate nonzero. Clean EOF between complete requests exits
zero. No internal input/output queue or growing frame history is introduced;
the transport retains one reusable input buffer plus the detector's existing
bounded state. A caller owns its queue bounds, response deadline and process
termination if its peer stops responding.

Each response is one flushed JSON line on stdout. It preserves the original
confirmed `pixel_measurement`, including its distinction from tentative
candidates, predicted tracks and the Kalman posterior. The additive
`rgb_stream` object echoes protocol/epoch/sequence/time/dimensions and reports
`source_kind="simulation"`, `timestamp_source="simulation_time_us"`,
`pixel_format="RGB3"`, per-frame sequence gaps, and host-monotonic receipt and
processing boundaries in microseconds. `processing_ms` covers original
detector processing and any configured visual motion, not pipe transfer or
response serialization. The clock domains are separate; subtracting simulation
time from a host timestamp is invalid. Existing `source_received_us` stays zero
because that device field has no physical-camera receipt in this interface.

NPU and target pose must be disabled. The original detector's existing armor
diagnostics may still run, but this profile declares color `auto` and consumers
use only the confirmed green image measurement. All outputs retain
`safe_for_control=false`, `angles_valid=false`, `pose.valid=false`, unknown
pixel covariance and `exposure_timestamp_valid=false`. A separate simulator
consumer may interpret pixels using its declared camera geometry; this does
not qualify the board or change the hardware fail-closed contract. stderr is
reserved for errors; retain it with a bounded consumer log.
