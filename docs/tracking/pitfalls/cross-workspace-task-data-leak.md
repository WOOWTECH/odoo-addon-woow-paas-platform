# Pitfall: Cross-workspace `project.task` data leak（CAQA B1）

> 一旦對 `base.group_user` 在 `project.project` 端做了 ACL 收緊，**必須**對等檢查 `project.task` 端的 Odoo std rule，否則整個 instance 所有非 followers-privacy task 對任何 base.group_user 都能 read。

## Symptom

任何 `base.group_user` 內部使用者，登入後對 `project.task` 走 ORM read 路徑（`search` / `browse().read()` / `read_group` / M2M widget / search domain），可以**讀到整個 Odoo instance 上所有 task** — 包括別的 workspace 的 name / description / chatter / extra_cloud_service_ids / 任意欄位。

- `search` 看得到 — 因為 record rule 在 search 層 OR-combined，permissive 那條腿命中即放行。
- `browse(id).read()` 看得到 — 同理，OR 路徑放行。
- 不影響 `write` / `create` / `unlink`（std `task_visibility_rule` 把這 3 個 perm 顯式設 False；自家 `project_task_rule_workspace_member` 仍把這 3 個 perm 限到 workspace）。
- 不影響 `sudo()` 路徑（sudo bypass 所有 record rule）。

**外觀**：使用者單元測試與功能測試全綠（25/25 AC pass），但 CAQA 走 security audit 才會發現。**這是「測試覆蓋率與真實 ACL 縮緊脫鉤」的典型場景** — plan §AC 只規範 `project.project` ACL（AC #4-6），沒寫 task 端 cross-workspace read 的 AC，所以 implementation follow plan、tester follow AC，沒人覆蓋 task 端。

## Why it happens

### OR 語意 + always-true leg + 新加 ACL CSV row

3 個條件同時成立：

1. **Odoo std `project.task_visibility_rule`** 套在 `base.group_user`，domain 含 `('project_id.privacy_visibility', '!=', 'followers')` 這條腿。Odoo project 預設 `privacy_visibility = 'employees'`，這條腿對所有 default-privacy 的 project 永遠為 true。
2. **本 refactor 為了 D3=A 的 unbound project 改寫**，在 `ir.model.access.csv:27` 給 `base.group_user` 第一次加上 `project.task` 的 RWCU 權限。Pre-refactor `base.group_user` 對 task 完全沒 access，所以 std rule 從不 evaluate；refactor 後 CSV 開放，rule 開始 evaluate。
3. **新增的自家 `project_task_rule_workspace_member`** 也套在 `base.group_user`。Odoo 對同 group 的多 rule 採 **OR**（`ir_rule.py:140-168`）。

→ 有效 read domain：

```
(std task_visibility_rule.domain)  OR  (project_task_rule_workspace_member.domain)
≡  (project.privacy != 'followers' OR follower)  OR  (workspace member)
≡  always-true 對 default-privacy='employees' project 的所有 task
```

### 為什麼 project 端有拔、task 端沒拔（不對稱的根因）

`18.0.1.0.22/post-migrate.py` 對 `project.project_public_members_rule` 做了 deactivate（這個動作本身正確）。但 task 端有**兩條** Odoo std rule（`task_visibility_rule` 套 `base.group_user`、`task_visibility_rule_project_user` 套 `project.group_project_user`），**漏拔**。

Implementation 在加 `ir.model.access.csv:27` 的 task RWCU row 時，沒意識到「對 base.group_user 第一次開放 task ACL = 觸發所有 task 端 std rule」這層因果。

**真正的根因不是「漏一條 SQL」，是「Project 端發現的 OR-bypass pattern 沒當 invariant 套用到 sub-model」**。Plan 已點出 project 端 leak（§現況 line 23），但 plan 沒有把「對所有 OR-bypass-able std rule 都檢查」寫成 deactivation invariant，所以 implementation 對等性 audit 沒做。

### 為什麼 25/25 AC 全綠也沒抓到

- plan §AC 內 AC #4-6 只規範 `project.project` 的 ACL，未涵蓋 `project.task` 跨 workspace read 行為。
- AC #9-13 規範 task 的 extras 欄位 / domain，是「task 內的某欄位行為」測試，**不是** task 跨 workspace read 測試。
- AC #11 雖然測 `'workspace_id' in domain`，但那是欄位 domain，不是 record rule。
- → AC 設計遺漏對應的 security AC，整套 TDD 流程內**沒地方會 fail**。CAQA 走 security audit 走獨立 cross-check 才補上。

### 為何難以肉眼看出

- `ir_rules.xml` 內**只看自家 3 條 rule** 都沒問題。
- Migration 內**只看 deactivate SQL** 也沒問題（拔的是正確的 std rule）。
- 必須**同時讀**：Odoo std `project_security.xml` 內所有 task 端 rule 的 group / always-true leg + 自家 ACL CSV + 自家 ir.rule + migration deactivate 的 IN list — 才能看出對等性缺漏。
- 4 個檔案在 4 個地方，個別 PR review 看不見全貌。

## How to avoid

### 強制 checklist：套 [[odoo-addon-std-rule-symmetric-deactivation]]

每次符合下面任一條件，跑完整 checklist：
- 在 `ir.model.access.csv` 給 `base.group_user` 新增 Odoo std model 的 RWCU
- 新增 `ir.rule` 套 `base.group_user` 對 Odoo std model
- 改既有 deactivate-std-rule 的 IN clause（拔一條或加一條都要再走 inventory）

Checklist 內容：
1. 該 model 的 Odoo std rule inventory（grep `addons/<module>/security/`）
2. 對等 sub-model 的 Odoo std rule inventory
3. 每條 rule 的 groups + domain + perm_*=False/True 列表
4. always-true leg 識別（任何 leg 是 `field != literal` 對 default 值常為 true 都要警覺）
5. 一次 deactivate 全部，single UPDATE + IN clause

### Code 層 invariant 提醒

- `src/migrations/18.0.1.0.23/post-migrate.py:93-107`：B1 fix 的對等 deactivate SQL，註解明確指向本 pitfall doc + CAQA report
- `src/security/ir_rules.xml:101-107`：自家 `project_task_rule_workspace_member` 的 inline comment 明說「若 future maintainer 重啟 std rule，這個 lockstep 立刻破」

未來改任一處的維護者要先看註解 cross-ref，再決定要不要動。

### Test 層守線

`src/tests/test_cross_workspace_task_isolation.py` 是這個 leak 的 regression lock：
- `test_alice_cannot_read_bob_task_via_orm`：search + browse.read 兩條 ORM 路徑各驗一次
- `test_bob_can_read_own_workspace_task`：own-workspace sanity，防 over-correction

未來改 task ACL 任何一條 rule，這 2 條 test 不通過就停下。

### Verify SQL（每次 migration 完跑）

```sql
SELECT d.name AS xmlid, r.name, r.active
FROM ir_rule r
JOIN ir_model_data d ON d.res_id = r.id AND d.model = 'ir.rule'
WHERE d.module = 'project'
  AND d.name IN (
    'project_public_members_rule',
    'task_visibility_rule',
    'task_visibility_rule_project_user'
  );
-- expect: 3 rows, all active = false
```

少於 3 row → Odoo patch 改 xmlid，**立刻停下不上 production**；3 row 但有 active=true → migration 沒跑成功 / 被 module update 還原。

### Hardening 建議（follow-up）

migration 加 row count 防呆，未來 Odoo patch 改 xmlid 不會 silent leak：

```python
cr.execute("SELECT COUNT(*) FROM ir_model_data WHERE module='project' AND name IN (...)")
expected_count = 3
actual = cr.fetchone()[0]
if actual != expected_count:
    raise RuntimeError(
        f"Odoo std rule xmlid drift detected: found {actual}, expected {expected_count}. "
        "B1 leak might silently return — abort migration."
    )
```

Reviewer cleanup #6 已列為候選，[[follow-ups]] 追蹤。

### Hardening: declaration-data ↔ migration 1:1 對稱（B-1 incident 2026-05-20）

Issue #48 PR 把 task pair 的 std rule deactivation 從 migration 抽出來搬到 `src/security/ir_rules.xml` declaration data 時，**只搬了 2 條 task rule，漏了 migration 內第 3 條** `project.project_public_members_rule`。Fresh install 路徑不跑 migration、只跑 declaration data 的 `<function>` → 第 3 條 std rule 在 fresh install 上**仍 active** → 自家 `project_rule_workspace_member` 與其 OR-combined → 跨 workspace `project.project` ORM read leak。**同源 pattern 重蹈（漏一條 std rule deactivation）**，只是這次表面在 project 端、不在 task 端。

Root cause chain：

1. Spec D9 / AC #29 字面只寫「task rule」兩條 — **spec 本身漏了第 3 條**
2. Implementer 忠實依 spec 實作 → declaration data `<function>` 只 eval 2 條 rule ref
3. Implementation 自驗 SQL 也只 query 2 條（mirror 自己的 list），自證自迴圈，沒抓到不對稱

跟 task-pair 原 incident 的差異：原 incident 是 migration 漏拔 task pair；本 incident 是 declaration data 漏拔 project rule。**上一節 Hardening 建議的 migration row count 防呆守不到 declaration data 路徑** — fresh install 不跑 migration。

**Lesson**：未來把 migration 邏輯搬到 declaration data（或 declaration data 內任何 std rule 對 `base.group_user` 的 batch deactivate）時：

- (a) **Inventory migration 內所有 std rule write** — `git log -p src/migrations/` 抓所有 deactivate / UPDATE std rule 的 statement
- (b) **Cross-check declaration data 是否 1:1 對應** — migration 內 deactivate 的每條 std rule 都必須在 declaration data 的 `<function>` / `<record>` ref list 出現
- (c) Single UPDATE + IN clause（migration form）≡ single `<function>` + ref list（declaration form）— **兩邊 list 必須同步**，任一邊改了都要 cross check 另一邊
- (d) **驗 SQL 不能 mirror 自己的 list** — 用 [[odoo-addon-std-rule-symmetric-deactivation]] 的 inventory query 獨立列出 Odoo std `addons/project/security/project_security.xml` 所有套 `base.group_user` 的 rule，與 declaration data 對比，**不要從自己的 deactivate list 倒推**
- (e) **Spec 階段就要走 inventory** — 本 incident 根因是 spec 漏寫，不是 implementer 錯。spec 內任何 `<function>` deactivate 範圍規定，都要強制過 [[odoo-addon-std-rule-symmetric-deactivation]] §(c) "single UPDATE + IN clause" 的對稱性 mental check

References：

- Spec D9 / AC #29 / OR-9：`docs/plans/2026-Q2/2026-05-19-project-detail-page-design.md`
- CAQA B-1 fix verified：`docs/archived/tracking/caqa-report-2026-05-20-project-detail-page-issue-48.md` §B-1
- Fix landed in `src/security/ir_rules.xml:230-233`（補第 3 條 ref 進 `<function>` eval list）— 同 PR 內

## Related

- [[odoo-addon-std-rule-symmetric-deactivation]] — 這個 leak 抽煉出來的可複用 pattern
- [[paas-workspace-member-acl-tightening]] — 完整收緊 base.group_user ACL 的三件套（CSV + ir.rule + symmetric deactivation）
- [[cloud-service-project-decouple]] §Invariants 4 — 三條 std rule 必須一直 active=false 的 invariant
- Code 引用：
  - `src/migrations/18.0.1.0.23/post-migrate.py:93-107`（fix 落點）
  - `src/security/ir_rules.xml:101-107`（自家 task rule + cross-ref comment）
  - `src/tests/test_cross_workspace_task_isolation.py`（regression test）
- Commit / report：
  - commit `9552cb7` — `fix(refactor): deactivate task-side std rules symmetrically (CAQA B1)`
  - commit `d570006` — `test: lock cross-workspace project.task read isolation (CAQA B1)`
  - `docs/decisions/reviews/caqa-2026-05-16.md §E` — 完整 BLOCKER 原始證據鏈
  - `docs/decisions/reviews/caqa-2026-05-16.md §B1 Re-review` — fix 驗證
- Odoo 上游證據：
  - `addons/project/security/project_security.xml:88-172`（被 deactivate 的 3 條 std rule 定義）
  - `addons/base/models/ir_rule.py:140-168`（OR 語意實作）
