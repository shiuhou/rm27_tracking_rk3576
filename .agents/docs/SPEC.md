# RM27 Tracking RK3576 — SPEC

本倉擁有 RK3576 通用影像採集、預處理、錄製、回放、校準工具及 Linux 綠燈 detector／tracker。
按 [README](../../README.md) 保留 CLI、Python 接口及原始 payload 的相容性。
綠燈 detector 以現有 RGB pipe 輸出逐幀像素觀測。任務組裝由上層管理。

- 區分來源序號／時間、接收時間與回放時鐘；未知曝光時間與同步維持未知。
- 錄製保留格式、尺寸、stride、payload 與來源配置；缺失或損壞應可識別。
- 主機測試、ARM64 userspace、實板取像、校準與長時性能分開驗收；
  合成測試、模擬或歷史 M3C 數據不能替代實板結果。
- RGB pipe 的階段診斷須明確 opt-in，預設 JSON 不新增診斷欄位。
  診斷僅觀察既有計算，不能改門檻、排序、候選或確認決策；除時間與新增診斷外，
  固定錄製逐幀輸出須一致。每幀峰值樣本最多128筆，完整事件計數、截斷及未遍歷數須可見。
  人工標註只供回放後評分，不得進入 detector 決策。

詳細驗收條件與已知限制見 [驗證章節](../../README.md#validation-and-remaining-hardware-requirements)。
