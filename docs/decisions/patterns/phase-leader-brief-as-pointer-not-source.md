---
name: phase-leader-brief-as-pointer-not-source
description: Phase leader 寫給 phase agent 的 brief 是導讀（pointer），不是 source-of-truth；agent 撞到 brief 與 plan / schema invariant / source-of-truth doc 不符時，trust source、回報 leader、不靠 brief 開工
metadata:
  type: pattern
---

# Pattern: Phase leader brief 是導讀（pointer），不是 source-of-truth

> 適用：maestri 多 phase agent pod 流程下，phase leader 撰寫 hand-off brief 給下一 phase agent（tester / implementer / caqa / reviewer / reporter）時。**Brief 是「請你看 X / 對齊 Y」的導讀**，而非 X / Y 的權威複寫。當 brief 與 plan signed-off decision / decision doc invariant / schema 實際 shape **不一致**時，下游 agent 必須 trust source-of-truth 不 trust brief，並回報 leader 校正。

## When to use

leader 在組裝 phase brief 時，常有以下漂移風險：

- Brief 用 prose 摘要 plan §Decision Sign-off，但**省略 mechanism 細節**或**用稍微錯的詞**描述 D1=A / D2=A / D3=A 的具體 outcome
- Brief 引用 decision doc § Invariants 條目時，**改寫敘述**（如把「workspace_id computed from cloud_service_id」改寫成「workspace-only project」shape）
- Brief 假設某個 mechanism（如「assert `create_uid = user`」）但 plan 拍板的是另一個（如 D2=A 走 `message_subscribe` follower 不動 `create_uid`）

→ 下游 phase agent（tester / impl）若**字面照 brief 開工**，會寫出與 plan signed-off decision 不符的 test / impl，被 reviewer / caqa 反推回來。

## Core invariant

**Phase leader brief 是 pointer，不是 source-of-truth。**

| Layer | 角色 | 權威度 |
|-------|------|--------|
| **Plan signed-off decision**（如 `2026-Q2/2026-05-28-...-plan.md` §Decision Sign-off） | source-of-truth | 最高 |
| **Decision doc invariants**（如 `cloud-service-project-decouple.md` §Invariants） | source-of-truth | 最高 |
| **Schema actual shape**（如 `project_project.py:43-48` `workspace_id` related-from-cloud_service_id） | source-of-truth | 最高 |
| Phase leader brief | pointer/導讀 | 二手 — 用來指路 + 排優先序，**不是**真理 |
| Brief 內部摘要 | 摘要 | 三手 — 簡化過、可能 drift |

→ 當 brief 與上面三條任一不符，**trust source-of-truth + 回報 leader 校正**。Agent 不該假裝 brief 是對的然後寫出錯的 test / impl。

## What it looks like

### 反例（brief 字面照搬 → 寫錯 test）

Cycle 6 leader 第 1 次 brief tester：

```
"T1: assert create_uid = alice"
```

→ 字面看：tester 應該寫 `self.assertEqual(project.create_uid, alice)` 鎖 `create_uid` 是 alice。

但 plan §Decision Sign-off 拍的是 **D2=A**（`sudo().create()` 後走 `message_subscribe` 補 follower），mechanism 是 **`sudo().create()` 讓 `create_uid = SUPERUSER`**，**不動 `create_uid`**。

→ tester 若字面照寫 → `create_uid` 永遠 SUPERUSER 不是 alice → T1 永紅 → tester debug 半天才發現「brief 摘要錯了，plan 拍的是 D2=A 不是 D2=B」→ 退回 leader 校正。

### 正例（tester 校正 brief，trust plan）

Cycle 6 tester 實際做：

1. **讀 brief**：「T1: assert create_uid = alice」
2. **讀 plan §Decision Sign-off**：「D2 = A: controller create 後 `message_subscribe` self user 為 follower」+ §Open Q2 「D2=A 走 follower 路徑，不動 create_uid」
3. **發現不一致**：brief 與 plan mechanism 不符（brief 似乎照 D2=B 寫，但 plan 拍 D2=A）
4. **校正後寫 T1**：assert `request.env.user.partner_id in project.message_partner_ids`（follower），**不**assert `create_uid = alice`
5. **docstring 留紀錄**：「T1 mechanism-coupled to D2=A `message_subscribe`. Note: leader brief mentioned `create_uid=alice` but plan §Decision Sign-off D2=A keeps `create_uid=SUPERUSER` and uses follower; following plan source-of-truth over brief摘要」

→ T1 寫對、首跑就紅在正確位置、leader 後續確認 brief 摘要漂移、不退回 tester。

### 另一個 cycle 6 instance

Leader 第 2 次 brief（reporter task brief）：

```
"workspace-only project shape"
```

但 plan §Invariant #3 + decision doc §Invariants 都寫：

```
3. `project.project.workspace_id` 必須維持 `compute='_compute_workspace_id'` + `store=True` +
   `related-style` 對 `cloud_service_id.workspace_id`
```

→ `workspace_id` 是 computed-from-cloud_service_id，**沒有「workspace-only project」這種 shape**。

正確判讀：「brief 寫 'workspace-only project' 是 prose 簡化，實際是 D1=A 的「cs / ws / unbound」三分支裡的「ws」分支 — `workspace_id` 仍從 `cloud_service_id` compute（因為 ws 分支可能 cs 為空 → workspace_id 也為空，或 cs 帶上 → workspace_id 從 cs 取）」。

reporter 跟 tester 都正確判讀 plan / decision doc，不 bend implementation 去製造一個「真的 workspace-only field」。

## Why this pattern

### Brief 是「請看 X / 排優先序」的工具，不是 X 本身

leader 寫 brief 的目的是：
1. **告訴 agent 該看哪些 doc**（plan / test report / caqa / reviewer / decision doc）
2. **告訴 agent 排哪些 task 的優先序**
3. **點出可能撞到的 trap / 注意事項**
4. **不重複** plan / decision doc 的完整內容（避免雙寫漂移 — brief 100-300 行 vs plan 500+ 行）

→ Brief 內的 prose 摘要、引用、決策描述都是**簡化過的 pointer**，必有信息損失。當 brief 與 source-of-truth 衝突時，**source-of-truth 必勝**。

### Plan-gap 守線的延伸（[[plan-gap-stop-and-report]]）

[[plan-gap-stop-and-report]] 規範了「agent 撞到 plan 沒設計的決策點 → 停下回報」。本 pattern 是同 spirit 的延伸：

| 情境 | 對應守線 |
|------|---------|
| Agent 撞到 **plan-level gap**（沒設計） | [[plan-gap-stop-and-report]] — 停下回報 |
| Agent 撞到 **test 假設錯** | [[plan-gap-stop-and-report]] §impl/test 層延伸 — 改 test 別 bend impl |
| Agent 撞到 **brief 摘要與 plan/schema 不符** | 本 pattern — trust source-of-truth + 回報 leader 校正 brief |

三者共通核心：**「surface 看起來對」不是 agent 的工作；agent 的工作是「實質對」**。Brief 字面 follow 屬於前者 — 寫出 test 跑得起來但 mechanism 錯。Trust source 屬於後者 — 寫出 test 真實鎖 outcome、首跑紅在正確位置。

## When NOT to escalate

不是每個 brief 細節漂移都要 escalate。判準：

| 情境 | 處理 |
|------|------|
| Brief 摘要與 plan **mechanism 不符**（如 D2=A vs D2=B mechanism 細節錯） | 停下校正 + 在 docstring 留紀錄 + ask leader 確認 |
| Brief 引用 **path / 行號漂移**（如「`ai_assistant.py:1228`」實際 `:1230`） | 不 escalate，直接看 source 找正確位置 |
| Brief 用 **不同字詞** 描述同一 concept（如「workspace-only」vs「ws 分支」） | 不 escalate，內部對齊 source 判讀 |
| Brief 寫 **plan 沒寫的事**（leader 額外要求） | 把「plan 沒寫的事」視為 leader 額外 instruction，照做（但不 promote 到 plan-level invariant） |

關鍵：**只在 brief 與 plan signed-off decision / decision doc invariant / schema 實際 shape 衝突時** trust source 並回報。

## Why not 合進 [[plan-gap-stop-and-report]]

兩個 pattern 都關於「不要在 surface 假裝對」，但**觸發 layer 不同**：

- **plan-gap-stop-and-report**：agent 在執行任務時，遇到「下游需要某東西但 plan 沒設計 / 我沒對應來源」→ 停下 + escalate
- **本 pattern**：agent 在**接收任務時**，brief 摘要與 source-of-truth 不符 → trust source + 校正 brief

前者是「agent 主動發現 gap」，後者是「agent 被動 detect brief drift」。前者的責任在 plan author，後者的責任在 brief writer（leader）。混在同一 doc 會稀釋兩個 pattern 各自的 invariant；分開能讓未來 leader 寫 brief 時對照本 pattern 自我審視。

## See also

- [[plan-gap-stop-and-report]] §impl/test 層延伸 — 同 spirit 守線在另一 layer
- [[plan-vs-odoo-schema-mismatch]] — plan 階段引用 Odoo schema 字面不對的事件（同 family 的 plan-side 漂移）
- `docs/plans/2026-Q2/2026-05-28-decouple-project-from-workspace-frontend-plan.md` §Decision Sign-off — cycle 6 plan signed-off decisions（source-of-truth example）
- `docs/decisions/refactor/cloud-service-project-decouple.md` §Invariants — cycle 1 decision doc invariants（source-of-truth example）
- maestri inter-agent hand-off 流程文檔（leader 寫 brief 時的 checklist）
