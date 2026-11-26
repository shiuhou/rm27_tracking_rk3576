# RM27 Tracking RK3576 — ROADMAP

| 節點 | 狀態與依賴 |
|---|---|
| 視覺組Git review（2026-11-26） | `codex/vision-team-review`收錄目前Linux detector／tracker、相機接入、診斷與同學導讀；主機來源檢查見README，main與硬體驗收範圍分開 |
| 通用採集、預處理、原始錄製與回放 | 已有 [host／wheel 回歸](../../README.md#validation-and-remaining-hardware-requirements) |
| ARM64 | [QEMU runtime 及 ABI 證據](../../README.md#arm64-installation-and-verified-scope) 各有原範圍，不能作為實板性能 |
| 固定錄製漏檢階段診斷（2026-11-23） | [有界 opt-in 診斷與離線比較](../../README.md#offline-stage-diagnostics) 已實作；PC 三種模式各1,281幀非計時一致，無新增掉幀。16均勻人工樣本仍0/16，框內1,560峰值全為外觀拒絕，未調參；3補充陽性分開計分。實板新版與一般化品質尚未驗收 |
| RK3576 取像與標定 | [相機／載板證據](../../README.md#rk3576-camera-evidence-and-first-boot) 隨實際配置更新；正式感測器的時序、標定、持續運行需獨立證據 |

[需求與驗收](SPEC.md)｜[設計](DESIGN.md)。Onboard P1 的整合結果由 Onboard 維護。
