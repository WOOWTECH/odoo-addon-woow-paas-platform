# Pattern: invariant 收斂在 ORM `create()` / `write()` hook，不只 transition method

> 適用：當一個 model 有「**跨同組 record 演算**的 derived flag / 不變式」（e.g. 至多一筆 `is_latest=True` per `(project_id, name)` group、winner by `(published_at desc, id desc)`），而現有設計只在 **user-facing transition method**（`action_publish` / `action_deprecate` / ...）末尾呼叫 recompute helper。**任何繞過 transition 的 ORM 寫入路徑都會 drift**：test fixture、demo / data XML、未來上傳 API、admin 匯入、程式化 import、cron。修法：把 recompute 也搬到 model `create()` / `write()` override，由 hook 在偵測 trigger key（如 `state` 變動）時兜底。transition method 仍是 user-facing 入口（含 illegal transition guard + side effects），hook 是「程式化捷徑兜底」—— 兩者並存、idempotent if-guard 避免噪音。

## When to use

當下列條件**同時**成立：

1. **存在 cross-record invariant**：某個 derived flag / 不變式的值不是 stored compute（無 `@api.depends` 可宣告），而是「對同組 N 個 record 演算後選 winner」這種跨筆邏輯。typical：「最新 published 版本旗標」「同 group 至多一筆 primary」「同 tenant 唯一 active row」。
2. **目前只在 user-facing transition method 收斂**（`action_X` / `confirm_Y` / `validate_Z`）末尾呼叫 `_recompute_invariant`。
3. **model 至少還有第二條 ORM 寫入入口**會踩中 trigger key。常見的「第二條入口」：
   - test fixture 直接 `create({'state': 'published'})` 跳過 action_*
   - 未來的 HTTP 上傳 API / programmatic import 寫入 model（design 已預告或 spec 已點名）
   - demo / data XML 預載 record（loader 是 raw ORM、不走 action_*）
   - admin / 維運走 odoo shell / RPC `write({...})`

→ 三者同時成立 → invariant 是「**latent drift bug**」：transition path 全綠、但程式化路徑會建出違反不變式的 record。即使該 invariant 目前**無消費端**（如本 epic：`is_latest` 暫未被讀），它仍是 data-correctness debt，後續一旦有人讀就會踩到不一致。

**不適用**：

- 單筆內可由 `@api.depends` 表達的 stored compute → 用 Odoo 既有機制，不要重做一層 hook。
- 真的只有 user-facing transition 一條入口 + 透過 ACL / view layer 物理上保證無 ORM 捷徑 → 不需 hook（但要 grep 確認，不要憑感覺）。
- 跨 model / 跨 module 才能算的 invariant → hook 只看 self model 不夠，需要另一條設計（e.g. `@api.constrains` + cross-model fetch、或 cron 重算）。

## What it looks like

### 反例 — 只在 transition method 收斂

```python
# models/skill.py — only action_* call recompute
def action_publish(self):
    self.ensure_one()
    if self.state != 'draft':
        raise UserError(...)
    self.write({'state': 'published', 'published_at': fields.Datetime.now()})
    self._recompute_is_latest(self.project_id.id, self.name)  # 只有這裡兜

def action_deprecate(self):
    ...
    self._recompute_is_latest(...)
```

**問題**：test fixture / demo data / 未來 upload API 走 `Skill.create({'state': 'published'})` 完全繞過 `action_publish` → `_recompute_is_latest` 從未呼叫 → drift skill（state=published 但 `is_latest=False`、`published_at=NULL`）落地。

### 正解 — hook 兜底所有 ORM 寫入入口

```python
@api.model_create_multi
def create(self, vals_list):
    skills = super().create(vals_list)
    for skill in skills:
        # 業務側 side effect 維持
        skill._anchor_bundle_attachment()
        # invariant 兜底：繞過 action_publish 直建 published 也補上
        if skill.state == 'published':
            if not skill.published_at:
                skill.published_at = fields.Datetime.now()  # 補在 recompute 前
            skill._recompute_is_latest(skill.project_id.id, skill.name)
    return skills

def write(self, vals):
    result = super().write(vals)
    if 'bundle_attachment_id' in vals:
        for skill in self:
            skill._anchor_bundle_attachment()
    # invariant 兜底：state 變動（含 programmatic write）都觸發 recompute
    if 'state' in vals:
        for skill in self:
            if skill.state == 'published' and not skill.published_at:
                skill.published_at = fields.Datetime.now()
            skill._recompute_is_latest(skill.project_id.id, skill.name)
    return result
```

`_recompute_is_latest` 內部：對 group 內每筆 record `if skill.is_latest != desired: skill.write({'is_latest': desired})` — vals 只含 `is_latest`、**不含 `state`** → 不踩 hook 條件 → 自然不遞迴。

### 三個關鍵細節

1. **trigger key 要狹**：write hook 條件綁 `'state' in vals`（觸發 key 必須**明確列在 vals**），不要綁 `skill.state == 'published'`（後者對「state 沒被改但是 published」也會觸發 → 每次 write any field 都重算）。
2. **published_at 補蓋順序**：必須**先**補 `published_at` **再**呼叫 `_recompute`，否則 winner sort 的 `key=lambda s: (s.published_at or epoch, s.id)` 把剛建的 published skill 排到墊底，winner 算錯。
3. **內部 write 不能含 trigger key**：`_recompute` 內部寫 `{'is_latest': ...}`，**絕不**寫 `{'state': ...}`。違反 → 無限遞迴。

## Why this pattern

- **單一維護點**：trigger key 是 `state`，trigger key 寫入的所有 ORM 路徑都被 `create()` / `write()` 涵蓋（這是 ORM 的物理事實，不靠紀律）。call site 不需要記得「呼 recompute」—— recompute 跟著 ORM 寫入自動發生。
- **transition method 仍負責 user-facing 語意**：illegal transition guard（從 archived 不可 publish）、額外 side effect、UI 反饋 …… 都還在 action_*。hook 不取代 transition，只在 ORM 寫入這層**兜底**。兩條同時生效時，recompute 算兩次（一次 action_* 一次 hook），但 `_recompute` 內 `if invariant != desired` idempotent guard 把第二次變 no-op → 安全且無噪音。
- **未來 input surface 自動覆蓋**：spec / design 已明示「未來會補 HTTP upload API」。upload endpoint 寫 model 仍走 ORM → hook 自動兜，**不需要 reviewer 在新 PR 提醒「記得呼 recompute」**。
- **migration 補資料是配套，不是替代**：對既有 drift 資料寫一次性 idempotent migration 補回，model hook 守線新建路徑。兩者覆蓋「歷史」與「未來」各一邊，缺一邊都會留破口。

## Anti-patterns（具體別做）

- ❌ **把 recompute 加在 view button method**：button method（`action_*`）只是 UI 一個入口；移到那裡跟「只在 transition method 收斂」同型，仍漏 ORM 捷徑。recompute 要在 model `create()` / `write()` 這層。
- ❌ **trigger key 綁 `skill.state == 'X'` 而非 `'X' in vals`**：對「state 沒改但是 X」的 record 每次 write 都重算 → 對 batch write、無關欄位 touch 都產生 noise SQL。trigger key 必須明確檢查「**這次 vals 是否含此 key**」。
- ❌ **內部 recompute 寫 vals 含 trigger key**：`self.write({'state': new_state, 'is_latest': True})` 在 `_recompute` 內 → 觸發自身 → 無限遞迴。recompute 內**只寫 derived flag**，不重寫 trigger。
- ❌ **published_at 補蓋在 recompute 之後**：winner sort 看到剛 create 的 record `published_at=NULL` → fallback epoch → 排到墊底 → 認錯 winner（通常是任何舊的 published record 仍當 latest，新建的卻不是）。**先補 timestamp、再 recompute**。
- ❌ **用 context flag（如 `with_context(skip_invariant=True)`）防遞迴**：能避免遞迴但有兩個壞處：(a) call site 容易漏 flag → drift 又回來；(b) `_recompute` 內部 write 從 vals 結構就不踩條件、不需要 flag。Flag 是「最後手段」、不是 default。

## 落地（本 epic）

- spec `docs/superpowers/specs/2026-05-28-skill-e2e-and-is-latest-drift-fix.md` §① model 層 — `create()` / `write()` 收斂 `_recompute_is_latest`
- impl commit `94a63a6` — `fix(skill): close is_latest drift on create/write to published state`（`src/models/skill.py:103-130`）
- 配套 migration commit `be75361` — `src/migrations/18.0.1.0.28/post-fix-is-latest.py`（既有資料的兜底）
- regression test commit `94a63a6`（同 commit）— `TestSkillIsLatestDriftRegression`（`src/tests/test_skill_is_latest_drift_regression.py`）6 case，鎖 hook 行為（含 `test_action_publish_still_works_after_hook` 確保不破 transition method 既有路徑）

## Invariants（後人改本區前必看）

- `_recompute_is_latest` 內部 write 的 vals **不得含** `state` —— 違反 → write hook 條件 `'state' in vals` 命中 → 無限遞迴。
- write hook 條件用 `'state' in vals`、**非** `skill.state == 'published'` —— 後者對 batch write 無關欄位產生噪音 SQL。
- `published_at` 補蓋必須在 `_recompute_is_latest` 呼叫之前 —— winner sort 依賴 `published_at`。
- transition method（`action_publish` / `action_deprecate` / `action_archive_skill`）內既有的 `_recompute_is_latest` 呼叫**不要拔**：拔了之後若把 hook 改回「只 create 不 write」或 hook 條件邊界微調，又會漏。兩條同時呼叫 idempotent，留著是 belt-and-braces。

## See also

- 配套 pitfall：[[httpcase-orm-flush-ordering-raw-sql]] — 在 HttpCase test 內製造 drift sample 時撞到 ORM cache/flush ordering 的雷。本 pattern 是「修法本身」，pitfall 是「為此 pattern 寫 regression test 時撞到的 fixture mechanics 坑」，兩者一組。
- 上下文 pattern：[[phase-versioned-test-assertion]] — 本 epic 同次 PR 對 Phase C `MIG.1` 由硬編 `18.0.1.0.27` 改 monotonic `>= 27` 的正面套用（manifest bump 27→28 不撞紅）。invariant hook + manifest bump + migration 三件事一條 PR 落地，二者搭檔證明 multi-phase / multi-cycle 演進的 test 守法。
- 守線 pattern：[[plan-gap-stop-and-report]] — tester 撞 fixture mechanics 雷時的處置（不 bend impl 湊綠、原樣回報，由 implementation 階段選方案）。本 PR 是該守線在「fixture mechanics layer」延伸 instance。
- 上下文（attacker-writable surface 上的 invariant）：[[constraint-input-trust-expansion]] — 該 pattern 處理「m2m 外人可寫 + ir.rule / constrains 用該 m2m 做 authority 推導」的 SQL-level invariant 兜法；本 pattern 處理「同 model 跨 record 演算的 derived flag」的 ORM-write-hook 兜法。兩者同屬「不能只靠 user-facing method 守 invariant」family。
- 原始來源（合成材料、不複製）：spec / plan / tester report / impl report / CAQA report / reviewer report — 路徑見 `docs/tracking/followups/follow-ups.md` cycle 6 索引。
