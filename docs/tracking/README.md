# tracking/ — 問題與追蹤

**未解問題、incident、技術債清單、測試報告**。需要長期觀察的東西放這。

## 進入條件

- 出現非預期狀態（incident、sandbox 故障、ACL 漏洞）
- 需要長期觀察或回頭對照（pitfall、follow-up）
- 測試覆蓋 / scope 報告（佐證 PR）

## 退出條件

- **長期資產不 archive**：pitfalls/、followups/ 留在 tracking/（被 CLAUDE.md / pattern-guard hook 常態引用）
- **一次性報告收官即歸檔**：CAQA / reviewer / test coverage / deliverables 報告在對應 cycle 收官後整批移 [`../archived/tracking/`](../archived/tracking/README.md)，**inbound 引用同步改寫**（2026-07-19 首批 42 檔已執行）

## 子目錄

| 路徑 | 內容 |
|------|------|
| [`issues/`](issues/) | 生產 / 沙盒問題排查紀錄（含修復方案 Path A/B） |
| [`pitfalls/`](pitfalls/) | 已踩過的坑（cross-workspace data leak、plan-vs-schema mismatch 等） |
| [`followups/`](followups/) | 技術債清單（單檔 `follow-ups.md`，F# 流水號跨 refactor cycle 連續編號） |
| [`test-reports/`](test-reports/) | **測試覆蓋 / 結果報告**：test scope report、coverage report、TDD red 解釋、CAQA test report |
| [`reports/`](reports/) | **交付物（deliverables）報告**：cycle / phase 收尾的 docs 清單 + commit message draft + PR description draft + leader hand-off 清單 + retro |

> **根層不放報告**：過往散在根層的 per-epic 快照檔（`caqa-report-*`、`reviewer-report-*`、`agent-runtime-pod-phase-*` 等）已於 2026-07-19 整批遷至 [`../archived/tracking/`](../archived/tracking/README.md)（42 檔，全 repo inbound 引用同步改寫）。**新報告一律寫進對應子目錄**（test-reports/、reports/——目前為空屬正常，寫入即重建），收官後批次歸檔。

## 命名

- `issues/`：`<incident-slug>.md`（已收官案例見 `../archived/tracking/`，如 `sandbox-hostpath-rot.md`）
- `pitfalls/`：`<incident-slug>.md`（如 `cross-workspace-task-data-leak.md`）
- `followups/`：固定 `follow-ups.md`，內部用 F1, F2, ... 連續編號（跨 cycle 不重置）
- `test-reports/`：`<feature>-<aspect>-YYYY-MM-DD.md` 或 `issue-<N>-test-coverage.md`

## 寫作慣例

### pitfalls/ — 無 frontmatter，採 H1 + sections 結構

CLAUDE.md `Known patterns` 直接以路徑引用 pitfall 檔，所以**不加 YAML frontmatter**（避免 grep / lychee 噪訊）。固定結構：

```markdown
# Pitfall: <title>

> Source: <如有 — promote 或 PR review ref>
> <一句話 summary：什麼情境會踩到 + 後果>

## Symptom
## Root Cause
## Fix
## Prevention
```

可選加 `## Timeline`（含 incident 時序）、`## Reproducer`（重現步驟）、`## Related`（連結 decision / pattern）。

### followups/follow-ups.md — F# 流水號 + 頂部 Index 表格

跨 refactor cycle 連續編號（F1, F2, F3, ...）。**新 entry 先 append 在文末（標記 `pending F#`），reporter 整理時統一指派編號並加進 Index 表格**。每條附 severity / origin / cycle / status。

檔案開頭（State 段之後、`## Index` 之前）維護一個 **「Active backlog 摘要」** block：只列尚未處理（status 非 done/resolved/closed）的 F#，按系列分組 + 一句話狀態，讓讀者開頭即可掃出所有 open/pending；已 RESOLVED/CLOSED 的 F# 不在此重列，但仍完整保留在 `## Index` 表原列（不刪除、不重編號）。每次新增或關閉 F# 時須同步更新此摘要。

### issues/ — 自由結構，重視可追蹤性

每筆 incident 含 **Symptom → Timeline → Root Cause → Fix（Path A/B 如有）→ Prevention**。改檔不刪原文，加 `**Update YYYY-MM-DD**:` 區塊保留 audit trail。

### test-reports/ — scope summary + result table

含測試範圍清單 + 通過 / 失敗統計表 + 連結回對應 plan / decision。多輪 review 各自獨立檔（如 `caqa-2026-05-16.md` + `caqa-2026-05-18.md`）。
