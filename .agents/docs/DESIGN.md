# RM27 Tracking RK3576 — DESIGN

[CameraSource](../../src/rm27_tracking/camera.py) 和
[RecordingSource 的回放模組](../../src/rm27_tracking/replay.py) 接到共用處理路徑。
[Runtime](../../src/rm27_tracking/runtime.py) 管理有界執行；
[Recording](../../src/rm27_tracking/recording.py) 保留原始 payload 與 metadata。
設計細節見 [One processing path](../../README.md#one-processing-path) 及
[Replay and timestamp provenance](../../README.md#replay-and-timestamp-provenance)。

沿用 [src/rm27_tracking/](../../src/rm27_tracking/)、[configs/](../../configs/)
與 [tests/](../../tests/)。[detector/](../../detector/) 維護 C++ 綠燈 detector／tracker、
OpenCV RGB 入口和原生回歸。Onboard 組裝 Python 影像管線與既有 RGB pipe。

`DetectorConfig::enable_stage_diagnostics` 預設關閉；RGB CLI 的 `--diagnostics`
啟用 `last_stage_diagnostics()`，以獨立 `stage_diagnostics` JSON 物件輸出。
搜尋矩形與峰值樣本為原始影像像素座標；region adapter 負責平移。
計數只記既有運算，樣本保留精算通過後實際走過的 NMS／外觀結果，最多128筆；
未遍歷及截斷另記。排程 coast 與 reset 清除當幀診斷，不能將快取候選當新觀測。
選中候選、通過關聯門檻與 tracker 確認分開呈現。人工框由
`detector/tools/video_replay/compare_recording.py` 回放後比對，不傳入 detector。
