# v0.2 计划落实与验收状态

更新日期：2026-11-04（America/New_York）。下方矩阵保留9月1日60fps基线；180fps业务状态见后文及
[业务验证报告](../reports/BUSINESS_FULL180_2026-09-08.md)。这里把“代码已实现”“工具链已实现”和“必须由真实数据/实机完成”
分开记录。缺少模型或真值时保持 fail-closed，不用占位数值制造通过结果。

后续算法主线已经按当前 1344×760@180 实现重新审查，见
[全视野 180 fps 绿灯识别更新方案](GREEN_DETECTION_PLAN_FULL180.md)。该方案指出现有
5 个 NV21 候选轮询到单一 tentative tracker 无法保证 100 ms 捕获，并将下一项代码工作
调整为全视野候选轨迹库、多尺度 proposal 和状态相关搜索调度；本页其余内容继续作为
已经完成事项和证据边界记录。使用者随后选择略过人工复核，后续可沿用 AI 暂定参考做回归诊断，
按具体错误层固定比较方法；不因此升级为独立准确率验收。本轮未实施这些算法修改。

## 2026-11-04：稀疏参考标注与品质评估第一阶段

工具与数据完整性 **PASS**；使用者明确选择略过人工复核，人工复核为 **NOT_RUN**，不再等待使用者标注。
AI 参考仍为暂定；正式品质评估因缺独立已验证真值而 **BLOCKED**，算法验收 **NOT_RUN**。
沿用原始四片及 45,462 行冻结辨识结果；23 个 source/config/binary 身份、manifest、原片和
JSONL 哈希核对，未重跑 detector、改变参数、接入飞控或操作硬件。
新增 `tools/dataset/prepare_reference_frames.py`；扩充既有 `tools/evaluate_target_reference.py`，
保持默认 green/armor 评估行为。固定源帧每 360 帧抽样，128 张均匀样本加 4 张诊断案例，
132 张原生 PNG 均与原片顺序解码逐像素一致。AI 查看无 detector 叠图的原图，近似中心／范围
均为 `ai_provisional`；无相邻帧插值。覆核页可人工修正并下载 JSONL，未确认列不自动升级。

均匀样本暂定为 **100 可见／24 不在画面／4 不确定**；可见样本中 82 个确认像素位置匹配、
12 个无候选、6 个有候选但无确认、0 个确认位置不符。后者 6 张有 3 张在 frame 0，
不能仅据此断言 tracker 故障。不在画面样本有 5 次新鲜确认输出，均对应背景门上方绿色小牌；
该负样本判断仍是 AI 暂定参考，不代替独立真值。82 个确认中心相对近似 AI 中心误差中位 0.968 px、最大 8.987 px，
沿用 `max(4, radius)` 匹配尺度，不是亚像素真值。
四个诊断案例另表：clip01/3160、clip03/5535、clip04/8681 可见但零候选；clip02/2112 不在画面且无输出。

位置输出层另有具体差异：clip02/10440 当帧确认像素误差 1.383 px，非预测 tracker 后验误差
52.037 px；两者分开记录。背景误确认、可见无候选及后验偏移列为优先覆核问题，
未将原因直接命名为阈值／关联／曝光／滤波问题。所有 45,462 行仅新增完整性、确认／预测和
无观测区间统计；不能把整段无观测区间当漏检时间或推定逐帧追踪连续性。

全部四片的两次完整稀疏评估结果一致，有序结果 SHA-256
`1bb5deea35cbb706fa6dafc4486efcf86cf879c8df378eeae9147c40f5d0d0e6`。
33 项相关工具测试在 OpenCV 4.6 和 5.0 均通过；原 6 项评估测试保持不变，
与 HEAD 旧算法在 120 行合成数据上的数值差分一致。旧 clip13 对应 replay 不在本机，
该真实旧模式复算为 NOT_RUN；参考 fixture 未改。
产物集中于 `artifacts/shared-real-green-quality/`，仅 132 张原生 PNG、4 份参考 JSONL、
样本清单、[覆核页](../artifacts/shared-real-green-quality/index.html)和
[单份摘要](../artifacts/shared-real-green-quality/summary.json)，小于 512 MiB。
命令与覆核步骤见 [README](../README.md#sparse-visual-quality-references-2026-11-04)。
这四片是已看过的回归数据，仍缺人工覆核及独立场景；RGB 回放不等于板端 NV21 或实机验收。
保留现有未提交工作，无 commit／push。

## 2026-11-04：四段共享真机影片与离线航向验证

原始 C++ 辨识／追踪已跑完四片共 **45,462 帧**，原片 SHA-256、影格顺序及名义时间检查 PASS。
其中 33,038 次新鲜确认像素观测；输出可用率不是 precision／recall，人工真值仍缺失。
三个已抽查到的可见漏识别案例仍保留，未因下游验证通过而改判。

Sim 复用这些原始输出及现有航向算法，完成 **45,462 笔文件式航向诊断 PASS**；
方向、30°/s 限速、0.3 秒过期保持及每片完整重跑一致性均通过。
Sim 相关测试 110 passed，完整回归 428 passed、5 skipped；不是本仓库的板端测试结果。
相机内参、前向安装及固定参考姿态均是明确的数值测试假设，`safe_for_control=false`。
实际飞机指令正确性、PX4 接收及硬件飞行为 NOT_RUN，仍需匹配标定和同步姿态。
这次未修改本仓库的 detector／tracker 核心或重定义旧的实机验收门槛。

同日补充已有结果的[视频可视化](../../../../../components/rm_uav_mujoco/artifacts/shared-real-heading-visualization/index.html)：
[clip01](../../../../../components/rm_uav_mujoco/artifacts/shared-real-heading-visualization/clip01/tracking_commands.mp4)、
[clip02](../../../../../components/rm_uav_mujoco/artifacts/shared-real-heading-visualization/clip02/tracking_commands.mp4)、
[clip03](../../../../../components/rm_uav_mujoco/artifacts/shared-real-heading-visualization/clip03/tracking_commands.mp4)、
[clip04](../../../../../components/rm_uav_mujoco/artifacts/shared-real-heading-visualization/clip04/tracking_commands.mp4)。
四片 H.264／1280×800／30 FPS 均完整解码、哈希核对 PASS，
保留完整名义时长，按每第六帧展示共 7,578 帧；[媒体摘要](../../../../../components/rm_uav_mujoco/artifacts/shared-real-heading-visualization/summary.json)保留来源与输出哈希。
画面区分当帧确认、沿用未过期观测及过期保持，并同步显示目标方位与限速航向曲线。
仅展示已有文件式诊断，不是飞机实测响应；未重跑辨识、执行新飞行或修改核心算法。

每片统计、漏识别样本及证据入口见 [共享影片回归](../README.md#shared-real-camera-test-videos)；
Sim README 保存执行命令和控制侧证据。本机 artifacts 不随 Git 分发；没有 commit／push。

## 落实矩阵

| 计划项 | 状态 | 实现或证据 |
| --- | --- | --- |
| 480×360@60、固定曝光/WB | 185 分钟帧率实测通过，标定待办 | 60.001 FPS、总处理 P95 17.37 ms；500 μs/手动 WB，曝光扫描仍待完成 |
| 归一化绿色响应和 2/4/6/9/14 px 多尺度 | 已实现/已测试 | tile 稀疏候选、定点 RGB 响应；5 px 合成目标及暗部编码噪声测试 |
| 最多 5 候选、±6.5° 捕获锥、取消中心软先验 | 已实现/已测试 | 全图 LAB 可记录锥外候选但不能控制；归一化密集搜索限制在控制锥；中心权重为 0 |
| 60 FPS CPU 调度优化 | 已实现/长时板测通过 | 480×360、传统/运动补偿 30 Hz、控制 60 Hz；185 min 为 60.001 FPS、总 P95 17.37 ms、估算丢帧率 0.0072% |
| 亚像素中心 | 已实现 | 多尺度响应加权中心与 Blob 几何/质心融合 |
| 红/蓝任意滚转双灯条 | 已实现/已测试 | PCA 主轴、平行/长度/色彩一致性/间距/绿灯固定关系；45° 合成测试 |
| 五关键点和装甲中心 | 已实现/已测试 | 统一点序；传统几何和 NPU 输出均映射到同一结构 |
| 平面 PnP、畸变和退化判定 | 已实现/已测试 | 完整端点、最小分离度、Brown 畸变、重投影误差；正面距离/退化测试 |
| 3 次有效后 100 ms 平滑切换 | 已实现/已测试 | `LAMP_APPROACH/FUSED/ARMOR_IMPACT` |
| YOLO11n-Pose ROI 验证 | 运行时代码和工具链完成；权重待训练 | 64–384 ROI、192/256/320 配置；启用后与传统检测隔帧交错，各 30 Hz；三候选轮询、空间一致性、fail-closed |
| 固定输入 INT8 AX620E/MUD | 转换工具完成；产物待生成 | ONNX 导出、100 图选择、Pulsar2 NPU2/NPU1 配置和模型 staging；必须先有标注权重 |
| 归一化视线 Kalman | 已实现/已测试 | 状态为视线 x/y、角速度、log 尺度/变化率；输出角速度和协方差 |
| KLT/光流 + RANSAC 全局补偿 | 已实现/已测试 | 稀疏角点、patch flow、亚像素 LK、相似变换 RANSAC；平移合成测试 |
| 最多 3 条候选轨迹 | 已实现 | 外观、尺度、速度、全局运动补偿、命中/丢失置信度；主控制仍只输出一条确认轨迹 |
| 五级状态机和 2 帧/35 ms 失效保护 | 已实现/已测试 | 远位置立即重捕获、过期预测继续报告但控制无效 |
| `MotionPrior` 与未来 IMU 接口 | 已实现/已测试 | 四元数、角速度、时间戳、视觉变换和插值；当前允许为空 |
| `TargetEstimate` 与 JSON schema v2 | 已实现/已测试 | 绿灯、装甲、位姿、LOS、状态、安全位、性能字段；保留 v0.1 平铺字段 |
| UART/CAN | 按计划不实现 | 后续直接序列化同一 `TargetEstimate` |
| ROI 导出、完整录像划分、增强、量化选择 | 已实现工具 | `tools/dataset/`；包含 3–20 px 主尺度、全角旋转、方向模糊、遮挡、色温、H.264 往返和结构复制 |
| 0/1 视频硬负样本 | 初始挖掘完成，人工复核待办 | 已从远离确认目标的候选导出 1224 个 ROI 到忽略目录 `datasets/hard_negatives_usb_0_1_v0.2` |
| 九段录像回归 | 已执行 | 最终 480×360 回归位于忽略目录 `recordings/maixcam2/2026-08-30_usb/results_v0.2_fast480_final_2026-09-01`；可提交摘要见性能报告 |
| 单元测试 | 已实现并通过 | 多尺度、捕获锥、任意滚转、PnP、NPU 门控、状态机、JSON、光流、IMU 插值 |
| 板端性能验收 | 无模型 185 分钟通过；温度/NPU 待办 | RSS 无增长且未见持续降频；日志未记录温度，真实模型尚不存在 |
| 300 次 15–25 m 最终试验 | 待采集 | 必须有距离、颜色、滚转、场地和运动真值，不能用九段无标注录像替代 |

## 当前不能伪造为“已完成”的外部产物

1. 原生 480×360 相机内参和畸变参数。
2. 固定镜头下的曝光/增益/WB 扫描结果。
3. 2026 实物灯条中心距、灯条长度和绿灯相对偏移。
4. 至少 1 万正 ROI、2 万硬负 ROI 的五关键点人工标注，以及独立第三场地测试集。
5. 训练后的 `best.pt`、固定输入 ONNX、Pulsar2 生成的两个 AXMODEL 和 MUD。
6. MaixCAM2 上模型 P95 和芯片温度数据（无模型 185 分钟 FPS/延迟/RSS 已通过）。
7. 300 次独立飞行/等效运动试验和角度真值。

这些输入完成前，`npu.enabled` 与 `target_geometry.pose_enabled` 必须保持关闭。启用 NPU 时建议
同时设置 `npu.required=true`，模型加载失败或模型结果与当前绿灯不一致即令
`safe_for_control=false`。

## 验收命令

```bash
cmake -S tests -B build/tests -DCMAKE_BUILD_TYPE=Release
cmake --build build/tests -j
ctest --test-dir build/tests --output-on-failure

python3 tools/run_v02_regression.py \
  --input-dir recordings/maixcam2/2026-08-30_usb \
  --output-dir recordings/maixcam2/2026-08-30_usb/results_v0.2

python3 tools/evaluate_replay.py RESULT_0.jsonl RESULT_1.jsonl \
  --annotations verified_intervals.json --enforce \
  --output-json reports/generated/replay.json \
  --output-markdown reports/generated/replay.md

python3 tools/evaluate_replay.py RESULT.jsonl \
  --ground-truth-jsonl verified_frame_ground_truth.jsonl --enforce \
  --output-json reports/generated/frame_metrics.json \
  --output-markdown reports/generated/frame_metrics.md

python3 tools/evaluate_device_log.py device_30min.jsonl \
  --output reports/generated/device_30min.json
```

`evaluate_replay.py --enforce` 只接受带人工核验区间或逐帧真值的录像；没有真值会明确失败。
区间真值负责捕获率/控制误报，逐帧真值还负责候选召回、关键点/角度误差和错误换轨。最终门槛：

- 100 ms 捕获成功率 ≥99%；
- 直接观测帧 ≥97%，融合控制有效帧 ≥99.5%；
- 捕获后控制无效间隔 ≤50 ms；
- 无目标区间 `safe_for_control` 次数为 0；
- 绿灯/装甲中心 P95 视线误差 ≤0.1°；
- 位姿重投影 ≤2 px、滚转误差 ≤3°；
- 板端总耗时 P95 ≤30 ms、模型 P95 ≤20 ms、丢帧率 <0.1%。

## 最终构建制品

最终代码已用 MaixCDK 为 `maixcam2` 交叉编译并打包；`file build/dart_green_detect`
确认是 ARM AArch64。安装包已显式携带 `nn` 间接需要的 ALSA 库，`main.sh` 在启动时
补齐其 SONAME 别名。当前无训练模型，因此安装包中的 `models/` 只有占位文件，默认配置
保持 NPU/位姿关闭；完成模型 staging 后必须重新执行 `maixcdk release -p maixcam2`。

```text
b81cfb3247886d94de336b52b6d9c4c86ffbbeca8e99a199ca8b9ef164bee714  build/dart_green_detect
91c647c7b4b7546992183836f29a30d27e92620eb4b7ae5f3dd2393280ef0e32  config/green_detector.conf
21cd97050fd7b34079bf74fbcb8103ef20d92eccc8d4002d9dd972f37f5c86da  dist/dart_green_detect_v0.2.0.zip
```

## v0.1.5 回退基线

本机保留的 v0.1.5 安装包为 `dist/dart_green_detect_v0.1.5.zip`，SHA-256：

```text
20fefaf0ae73ee46171b7a7eb429067b9a5645fe8bca65469c73f2f13215c44a
```

`dist/` 默认不提交 GitHub。发布 v0.2 前应把该包复制到团队制品库并记录板端系统、MaixPy、
MaixCDK commit 与配置版本；不要依赖某一台开发机的忽略目录作为长期回退方案。

## 2026-09-08：独立全视野180fps业务接入

原480×360稳定配置未改。新增最新帧槽、VIN DMA lease、NV21全视野候选和原图ROI精定位，
复用v0.2候选/跟踪/状态与JSON；输出线程独立180Hz。逐测量失败计数与时间年龄分离。
新增独立配置强制未标定控制失效，NPU/pose关闭，不进行动态模式切换。

主机四组CTest、ASan/UBSan/LSan/TSan及14项Python工具回归通过。最终候选两分钟
采集180.136fps、零上游异常，绿灯/装甲检测调用89.397/59.920Hz，TargetEstimate生成
180.004Hz，接收后源年龄P95 21.478ms。此前长测836秒出现291帧缺失，失败被保留；
已隔离采集/视觉/控制线程的日志磁盘I/O，使用固定容量缓冲并检查溢出及写失败。
最终30分钟复测324245帧、180.135791fps、零测量序号异常，全部324610个含预热lease归还；
绿灯/装甲调用88.672/59.792Hz，TargetEstimate生成179.998Hz，软件源年龄P95 22.992ms，
最高温66.984°C、MIPI错误0。RSS首末34032/34716KB，最后10分钟无持续单向上升；
保留小幅内存变化和最大软件年龄90.450ms的限制。原始相机与启动器恢复复验通过。
完整阶段结果和退出恢复见[业务验证报告](../reports/BUSINESS_FULL180_2026-09-08.md)。

长测中检测器自报直接观测10.433Hz，但无人工真值；目标可见时直接测量≥60Hz尚未验收。
检测调用频率不能代替真实观测频率或准确率。公共Camera/IVPS、
曝光端到端延迟、捕获后失效间隔、最大跟踪ROI、1344×760标定和比赛数据验收仍待完成。
NPU接口沿用原v0.2，真实模型及180fps架构中的异步NPU工作线程未接通；UART/CAN未实现。

### 同日检测调度提频试验

原88.67/59.79Hz结果受90/60Hz主动调度上限约束。新增可配置阶段频率及独立120/90、
180/120实验配置，原full180和480×360配置不变。当前场景180/120档两分钟实测
176.14/119.33Hz，输入180.135fps、零缺号，DMA全部归还，软件接收后源年龄P95 12.255ms。
该场景无确认目标、未覆盖最大跟踪ROI，新档未完成30分钟复验，不替代此前稳定性结论。
详见[调度提频报告](../reports/PERFORMANCE_FULL180_RATES_2026-09-08.md)。

## 2026-10-21：确认像素测量接口与主机数值验证

新增 `TargetEstimate.pixel_measurement`，从本次真实检测中唯一选中且经原跟踪器确认的
green candidate 导出跟踪位置修正前的原始中心；原 `green.center_x/y` 保留 TemporalTracker
后验语义。正常 camera 与 full180 VIN/cached 入口已接入实际源时间/序号标记和 JSON，
预测、跳过、未确认或重复来源不会变为新像素测量。未知协方差保持无效，不提升未标定的
`safe_for_control`、`angles_valid`、NPU 或 pose 状态。

显式 `highfps.green_hz==camera.fps` 时按每个新来源帧调度，默认 90 Hz 路径仍保留原
deadline。逆序包不再污染缺号高水位统计；新序号要求严格递增接收时钟。协方差接口严格
检查有限值、精确对称及 PSD，拒绝小量级不定输入，不将仿真噪声值写入硬件默认配置。
提交入口限单 producer 或全调用外部串行；交付只提供来源和年龄，消费者须独立检查新鲜度。
完整契约与坐标/噪声单位见 [像素测量接口](PIXEL_MEASUREMENT_INTERFACE.md)。

本次验证状态：

- **PASS — 主机数值接口**：`tests/run_pixel_measurement_host.py --compiler-bin C:/mingw64/bin`
  使用 GNU C++17 和现有 RGB 像素 stub，验证 raw/posterior 分离、确认/预测/一次交付、
  JSON 往返、180/90 Hz 调度、序号恢复、相同接收时钟拒绝、PSD 反例及 near-zero 合法矩阵。
  数值 NV21 helper 提取范围与哈希、运行记录位于忽略目录 `build/pixel-measurement-host/`。
- **NOT_RUN — 标准 CMake/CTest 完整 suite**：本次主机 `cmake` 不可用；未安装工具链。
  完整线程所有权/异步服务未运行，数值 fallback 不替代它们。
- **NOT_RUN — 本次改动的 ARM/MaixCDK 构建、实际 VIN/180 Hz 采集、板端延迟和闭环**：
  未访问设备，未据此更新此前硬件性能结论；本节只证明接口和主机数值合同。

### 无状态 RGB 候选接口

新增独立 `GreenLightDetector::detect_candidates(Image&) const`，返回当前 RGB 帧的
未选择候选，供外部已拥有姿态补偿/关联状态的消费者按模块接入。该方法使用新的局部
collector，不执行 TemporalTracker、候选轨迹库、NPU、装甲、确认或输出预测，也不修改
已有 detector 的跟踪状态和 `last_candidates()`。没有赋予候选任何控制安全、协方差或
源时钟语义；契约追加在现有像素接口文档中，原硬件配置和安全默认值保持。

- **PASS — 主机数值回归**：现有 `tests/run_pixel_measurement_host.py` 实际运行通过，
  新增同图候选不依赖旧轨迹、调用不推进跟踪状态/缓存和 selected 始终为 false 的检查。
- **NOT_RUN — 完整 CMake/CTest、ARM/MaixCDK、VIN/ISP/NPU 与板端延迟**：本机没有
  可用 CMake，未访问设备。本接口的候选生产不等于下游闭环或板端实时性验收。
