# tracking/ — 問題與追蹤

**未解問題、incident、技術債清單、測試報告**。需要長期觀察的東西放這。

## 進入條件

- 出現非預期狀態（incident、sandbox 故障、ACL 漏洞）
- 需要長期觀察或回頭對照（pitfall、follow-up）
- 測試覆蓋 / scope 報告（佐證 PR）

## 退出條件

- 議題 close、follow-up 完成 → **仍留在 tracking/**（不 archive，作歷史紀錄）
- 唯一搬走情境：超過 2 年且確認再無回查需求 → `archived/`

## 子目錄

| 路徑 | 內容 |
|------|------|
| [`issues/`](issues/) | 生產 / 沙盒問題排查紀錄（含修復方案 Path A/B） |
| [`pitfalls/`](pitfalls/) | 已踩過的坑（cross-workspace data leak、plan-vs-schema mismatch 等） |
| [`followups/`](followups/) | 技術債清單（單檔 `follow-ups.md`，每項 H2 + 日期） |
| [`test-reports/`](test-reports/) | Test scope report、coverage report、CAQA test report |

## 命名

- `issues/`：`<incident-slug>.md`（如 `sandbox-hostpath-rot.md`、`stg-networkpolicy.md`）
- `pitfalls/`：`<incident-slug>.md`（如 `cross-workspace-task-data-leak.md`）
- `followups/`：固定 `follow-ups.md`，每項 H2 + 日期標題
- `test-reports/`：`<feature>-<aspect>-YYYY-MM-DD.md` 或 `issue-<N>-test-coverage.md`

## 寫作慣例

- 每筆 incident 含 **Timeline → Root Cause → Fix → Prevention**
- Pitfall 必須含「Anti-pattern」段落（讀者第一眼就能避免）
- follow-up 條目格式：H2 標題 + 日期 + responsible owner + status（open / done）
- test-reports 含 scope summary + result table，連結回對應 plan / decision
- 改檔不刪原文，加 `**Update YYYY-MM-DD**:` 區塊（保留 audit trail）
