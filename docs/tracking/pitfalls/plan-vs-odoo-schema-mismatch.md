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
  - `docs/refactor/plan-cloud-service-project-decouple.md` §D3=A line 131
  - `docs/refactor/plan-cloud-service-project-decouple.md` §Open Q1
- Odoo 上游：
  - `addons/project/models/project.py`（Odoo 18 `project.project` 真實欄位 inventory）
