# cloudflare-security-audit（vendored）

來源：<https://github.com/cloudflare/security-audit-skill>，`skills/security-audit/`，commit **c1c8a8c**
（2026-09-14）。授權：MIT，Copyright (c) 2025-2026 Cloudflare, Inc.，原文見同目錄 `LICENSE`。

本目錄的 `.md` 一律**逐字保留**，`ticket.py` 從這裡擷取段落組 deep review 與驗證者工單。
改動只有兩處：

- 原本的 `SKILL.md` 改名成 `SECURITY-AUDIT-SKILL.md`，避免被當成本 bundle 的 skill。
- 沒有收 `validate-*.cjs` 與 `report-schema.json`：sweep 用自己的回報格式（見 `skills/sec-dispatch/SKILL.md`）。

## 我們用了哪些段落

| 段落 | 用途 |
|---|---|
| `HUNTING.md` 的 Core hunting method、Core validation rules | deep review 工單的方法與 finding 成立條件 |
| `ATTACK-CLASSES.md` 各類別、companion 檔的 Core discipline／類別／Universal moves／Validation rules | 依 `targets/<repo>.yaml` 的 `review_classes` 原文放進工單 |
| `SECURITY-AUDIT-SKILL.md` 的 severity anchors | 嚴重度判準 |
| `VALIDATION-AND-REPORTING.md` 的 Candidate-verifier prompt、Phase 5 record checks | 驗證者工單 |

沒有採用的部分（試跑結論見 #2064）：偵察 agent（改用 targets yaml）、coverage ledger 的 canonical ID
與 validator（改用 entry point 清單）、sandbox 與 artifact promotion（sweep 不執行目標程式碼）。

## 更新

換版時整個目錄重新複製，改 commit 號，跑 `tests/test_ticket.py`（會檢查用到的段落都還找得到）。
