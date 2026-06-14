# Pitfall: Plan 用詞與 Odoo 真實 schema 不符（AC #5）

> Plan 階段引用了 Odoo 標準 model 上**不存在**的欄位（`project.project.user_ids`），整套 plan / test / implementation 跑到 tester 才炸。Planner 必須 verify Odoo 真實 schema，不靠記憶 / 直覺。

## Symptom

Tester 在 red phase 把 plan §D3=A 的字面 `user_ids` 寫進 fixture：

```python
project.write({'user_ids': [(4, bob.id)]})
```

Odoo 拋 `ValueError: Invalid field 'user_ids' on model 'project.project'`。

Tester 一開始以為是 fixture bug 想繞過，CAQA 提示後才發現：**plan §D3=A line 131 寫的 `user_ids` 在 Odoo 18 `project.project` 不存在**。Odoo 18 `project.project` 的「成員」候選欄位是：
- `favorite_user_ids`（收藏的使用者，與 access 無關）
- `collaborator_ids`（內部 collaborator，與 access 無關）
- `message_partner_ids`（mail.thread follower，pre-existing）

Plan 用「user_ids」這個常見 Odoo 欄位名（在 `project.task` / `res.groups` / etc 都存在）寫到了 `project.project`，沒人在 plan review 階段交叉驗證。

## Why it happens

### Plan 階段的隱性假設

Planner 寫 plan 時靠對 Odoo 慣例的記憶：
- `project.task.user_ids` 確實存在（Odoo 18 task assignees）
- `res.groups.users` / `users_id` 等也常見
- → 直覺：`project.project.user_ids` 應該也存在

但 Odoo 18 把 `project.project` 的成員概念分散到 3 個欄位（favorite / collaborator / follower），沒有「`user_ids` 直接成員」這個欄位。**這是 Odoo 18 的設計選擇**，不是慣例。

### 為什麼 review / planner sign-off 沒抓到

- Plan reviewer（使用者）看 plan 是看「決策合不合理」、「scope 合不合適」，不會 mentally 對每個欄位名跑一次 Odoo schema lookup。
- Plan 階段沒有「verify schema」這道 gate。

### 為什麼 25/25 紅燈 mapping 沒抓到

- Tester 紅燈階段就是發現問題的地方 — 寫 `user_ids` fixture 直接炸。
- Plan §Open Q1 留了「user_ids 或 message_partner_ids 都可」的開放性，**但兩個 candidate 之一根本不存在**，這個開放性是假的。
- Test scope report 把「dual-add user_ids + message_partner_ids」當 portability 寫進 §Open Q1 處置。修 fixture 時才發現 dual-add 站不住。

### 影響面

- AC #5 紅燈推遲到 commit `b582a44` 才修正（移除 `user_ids` 行、僅保留 `message_partner_ids`），實作端對應改成 `message_partner_ids + create_uid` 雙腿。
- 沒造成 production leak（plan / implementation / test 都沒上 prod 前發現），但**消耗了一個 phase 的人力 + 額外 commit**。
- 若 Open Q1 拍成「實作走 user_ids leg」（plan 留的可能性之一），implementation 會直接寫不存在的欄位，造成 module 直接 install 失敗。

## How to avoid

### Planner 自我檢查 SOP（plan 觸及 Odoo std model 欄位時）

1. **Plan 內 grep**：plan 描述觸及哪個 Odoo std model 的哪個欄位？列出來。
2. **打開 Odoo 對應 model 檔**：本 instance 是 `/Users/eugene/Documents/woow/AREA-odoo/odoo-18.0/odoo/addons/<module>/models/<model>.py`，找 `_name = '<model.name>'` 對應的 class，verify 每個 plan 內提到的欄位確實存在。
3. **特別注意**：
   - `*_ids` 結尾不等於存在（Odoo 不同 model 對「相關 user」用不同欄位名）
   - 同名欄位在不同 model 上語義不同（task vs project 的 `user_ids` 完全不同事物）
   - Plan 寫「可選 X 或 Y」前，要 verify X 與 Y **都**存在

### Plan template 加 schema verification 段落

對所有涉及 Odoo std model 欄位的 decision，plan 須列：

```markdown
| Field | Model | Type | Source verification |
|-------|-------|------|---------------------|
| `cloud_service_id` | `project.project` | Many2one | self-owned, `src/models/project_project.py:7-13` |
| `message_partner_ids` | `project.project` | Many2many | Odoo std mail.thread, `addons/mail/models/mail_thread.py` |
| `create_uid` | `project.project` | Many2one | Odoo std abstract `models.Model` |
```

每行都要 verify，不空寫。

### Tester 防呆

tester 收到 plan 進 red phase 時，**第一步**先把 plan 內 Odoo std model 欄位 cross-check 過 — 任何懷疑就用 Odoo shell：

```python
self.env['project.project']._fields.keys()
```

或直接 grep Odoo source。**比寫 fixture 再炸再 debug 便宜得多**。

### CLAUDE.md 提醒（提議）

對 `paas-platform` 這個 Odoo 18 addon 專案，CLAUDE.md 可加一行：

> Odoo std model 欄位變動由 Odoo 版本決定，不假設 Odoo 17 / 16 的欄位仍在 18 上。Plan / test / implementation 觸及 Odoo std model 欄位時，**verify against the Odoo source under** `/Users/eugene/Documents/woow/AREA-odoo/odoo-18.0/`，**不靠記憶 / 直覺**。

實際是否加，[[follow-ups]] 列為 leader 與使用者確認題。

## Related

- [[cloud-service-project-decouple]] §9 Q1 — 拍板 `message_partner_ids + create_uid` 雙腿，正是這個 incident 的修正點
- Code 引用：
  - `src/security/ir_rules.xml:68`（自家 rule domain，移除 user_ids 走 message_partner_ids + create_uid）
  - `src/tests/test_project_decouple.py`（test fixture 在 b582a44 修正）
- Commit：
  - `b582a44` — `test: align AC #5 helper with Odoo 18 project.project schema`
- Plan 內原始引文：
  - `docs/plans/2026-Q2/2026-05-16-cloud-service-project-decouple-plan.md` §D3=A line 131
  - `docs/plans/2026-Q2/2026-05-16-cloud-service-project-decouple-plan.md` §Open Q1
- Odoo 上游：
  - `addons/project/models/project.py`（Odoo 18 `project.project` 真實欄位 inventory）

---

## 第二實例（2026-06-14）：agent teardown D1+D2 的 schema / 契約落差

> 同一個坑的擴大版。實作 agent runtime teardown（D1+D2）時，**前序調查摘要 + 計畫**對真實 schema 與 client 契約有 **6 處假設落差**，全靠「動手前先讀真實程式碼」（reading-first）逐一抓到。教訓不只「欄位存不存在」，而是**欄位的型態 / ondelete 行為 / 關聯方向 / cascade 鏈中段 / 例外類別命名 / 檔名**都不能靠記憶。

### Symptom（假設 vs 真實）

| # | 假設（錯） | 真實 | 證據 |
|---|---|---|---|
| 1 | `project.workspace_id` 是 `ondelete='cascade'` 的 **FK** | 是 **computed stored** 欄位（`compute='_compute_workspace_id'`，源自 `cloud_service_id.workspace_id`） | `src/models/project_project.py:44-49` |
| 2 | 刪 workspace 會 PG-cascade 刪 project（→ 需 workspace unlink hook） | `workspace→cloud_service` 雖 cascade，但 `cloud_service↔project` 是 **M2M**，**刪 workspace 不刪 project/session** → 不需 workspace hook | `project_project.py:11-17`（M2M `project_cloud_service_rel`）、`cloud_service.py:62-67` |
| 3 | project 有 `agent_session_ids` One2many（可直接走訪） | **不存在**，須 `agent_session.search([('project_id','=',...)])` | grep 全 `src/models/` 無 `agent_session_ids` |
| 4 | Odoo 檔是 `src/models/project.py` | 真實是 **`project_project.py`**（`project.py` 不存在） | `ls src/models/` |
| 5 | client 例外有 `AgentRuntimeUnavailable` / `AgentRuntimeError` / `AgentRuntimeBadRequest` | 只有 **`AgentRuntimeOperatorError`**（4xx/5xx 共用一類） | `src/services/agent_runtime_client.py:75` |
| 6 | operator `KubernetesService.delete_namespace()` 已存在 | **不存在**，須新增（仿 `delete_service`）；端點是 **同步**、例外是 `HelmException`/`KubectlException`（未處理 → 500，需顯式 raise 502） | `extra/paas-operator/src/services/helm.py`（grep 無 `delete_namespace`） |

> ⚠️ 第 1、2 點直接呼應本文件 §How to avoid line 68 —— 那張範例表把 `project.workspace_id` 當「self-owned Many2one」舉例，但它**其實是 computed**；而「workspace 刪除會連帶孤兒化 agent pod」更是錯誤的 cascade 鏈推論。**同一欄位、同一份文件、第二次中招**，足見不靠記憶的紀律有多必要。

### How to avoid（在第一實例 SOP 上補強）

第一實例的 SOP（grep plan 觸及的欄位 → 打開 model 檔 verify 存在）仍適用，**再加三條**：

1. **不只 verify「存在」，還要 verify「行為」**：`ondelete`（cascade / set null / restrict）、`compute`/`store`（是不是真 FK）、關聯方向（One2many 反向欄位是否真的定義在那個 model 上）。
2. **跨 model cascade 鏈要逐段走完**：A→B cascade + B→C cascade **不等於** A→C cascade —— 中間只要有一段是 **M2M**（如 `cloud_service↔project`），cascade 就斷在那裡。
3. **例外類別 / 方法 / 檔名一律重讀，不靠 grep 摘要**：跨 session 壓縮或前序 agent 的 grep 輸出可能 garbled（本次 `AgentRuntimeUnavailable` 等三個類名是憑空生成的）。在 `import`/`except`/呼叫它之前，打開該檔確認確切名稱。

### Related（第二實例）

- Commits：`91ee778`（D1 operator 兩步驟 teardown）、`e5d458d`（D2 Odoo 孤兒防護）
- PR：Gitea #195 → develop
- 設計：`docs/plans/2026-Q2/agent-runtime-pod-design.md` §2.6 / §8.4
- 真實 schema 來源：`src/models/project_project.py`、`src/models/agent_session.py`、`src/models/workspace.py`、`src/models/cloud_service.py`、`src/services/agent_runtime_client.py`、`extra/paas-operator/src/services/helm.py`
