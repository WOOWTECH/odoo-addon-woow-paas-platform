# drift-reaped GC eligibility 修復 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.

**Goal:** 保證任何進入 `state='reaped'` 的 agent_session 都有 `reaped_since`，讓 long-term GC（D3）能回收 drift / webhook 路徑造成的 reaped session。

**Architecture:** 在 `agent_session.write()` 集中強制 invariant「`reaped_since` 有值 ⟺ `state=='reaped'`」（stamp-on-enter + clear-on-leave），一處覆蓋 reaper/drift/webhook/未來路徑；migration backfill 既有 `reaped+NULL` 列。

**Tech Stack:** Odoo 18 ORM（model write override）、Odoo migration（post-migrate raw SQL）、TransactionCase + mock。

設計：`docs/superpowers/specs/2026-06-14-drift-reaped-gc-eligibility-design.md`。

---

## Task 1: write() override — stamp-on-enter + clear-on-leave

**Files:**
- Modify: `src/models/agent_session.py`（在 `create()` 後新增 `write()`）
- Test: `src/tests/test_agent_reaped_since_invariant.py`（新增）

- [ ] **Step 1: 寫 failing tests**

`src/tests/test_agent_reaped_since_invariant.py`：

```python
import os
from datetime import timedelta
from unittest.mock import MagicMock, patch

from odoo import fields
from odoo.tests.common import TransactionCase, tagged

_ENV_NAMESPACE_SECRET = 'AGENT_RUNTIME_NAMESPACE_SECRET'
_CLIENT_PATH = (
    "odoo.addons.woow_paas_platform.services."
    "agent_runtime_client.get_agent_runtime_client"
)


@tagged('post_install', '-at_install')
class TestReapedSinceInvariant(TransactionCase):
    def setUp(self):
        super().setUp()
        os.environ[_ENV_NAMESPACE_SECRET] = 'unit-test-secret-0123456789'
        self.addCleanup(os.environ.pop, _ENV_NAMESPACE_SECRET, None)
        self.Session = self.env['woow_paas_platform.agent_session'].sudo()
        self.project = self.env['project.project'].sudo().create({'name': 'RS'})
        self.session = self.Session.create({'project_id': self.project.id})

    def test_drift_style_write_stamps_reaped_since(self):
        # drift / webhook 只帶 state，無 reaped_since
        self.assertFalse(self.session.reaped_since)
        self.session.write({'state': 'reaped'})
        self.assertEqual(self.session.state, 'reaped')
        self.assertTrue(self.session.reaped_since)  # ← 被 override 補上

    def test_does_not_overwrite_existing_reaped_since(self):
        old = fields.Datetime.now() - timedelta(days=5)
        self.session.write({'state': 'reaped', 'reaped_since': old})
        # 再被 drift 寫一次 state='reaped'（無 reaped_since）→ 保留原值
        self.session.write({'state': 'reaped'})
        self.assertEqual(self.session.reaped_since, old)

    def test_explicit_reaped_since_in_vals_wins(self):
        explicit = fields.Datetime.now() - timedelta(days=3)
        self.session.write({'state': 'reaped', 'reaped_since': explicit})
        self.assertEqual(self.session.reaped_since, explicit)

    def test_leaving_reaped_clears_reaped_since(self):
        self.session.write({'state': 'reaped'})
        self.assertTrue(self.session.reaped_since)
        self.session.write({'state': 'active'})  # resume
        self.assertFalse(self.session.reaped_since)

    def test_re_reap_after_resume_gets_fresh_reaped_since(self):
        self.session.write({'state': 'reaped'})
        self.session.write({'state': 'active'})    # 清空
        self.assertFalse(self.session.reaped_since)
        self.session.write({'state': 'reaped'})    # 重新 reap → 新值
        self.assertTrue(self.session.reaped_since)

    def test_reaper_still_sets_reaped_since(self):
        # 回歸：reaper 同 vals 帶 reaped_since，不被覆蓋
        self.session.write({'state': 'active', 'last_active':
                            fields.Datetime.now() - timedelta(hours=2)})
        self.session.project_namespace_hash  # ensure hash
        client = MagicMock()
        with patch(_CLIENT_PATH, return_value=client):
            self.Session._cron_idle_reaper()
        self.assertEqual(self.session.state, 'reaped')
        self.assertTrue(self.session.reaped_since)
        client.reap.assert_called_once()

    def test_gc_now_collects_drift_reaped_session(self):
        # 本 gap 的整合守線：drift-style reaped → GC-eligible
        self.env['ir.config_parameter'].sudo().set_param(
            'woow_paas_platform.agent_gc_seconds', '2592000')
        self.session.write({'state': 'reaped'})       # drift-style，override 補 now
        self.session.write({'reaped_since':
                            fields.Datetime.now() - timedelta(days=31)})  # 催熟
        client = MagicMock()
        with patch(_CLIENT_PATH, return_value=client):
            self.Session._cron_gc_reaped()
        client.delete.assert_called_once_with(self.session.project_namespace_hash)
        self.assertEqual(self.session.state, 'absent')
```

- [ ] **Step 2: 跑測試確認 fail**

Run（沙盒）：`test --module woow_paas_platform`（過濾 TestReapedSinceInvariant）
Expected: FAIL（write override 未實作 → `test_drift_style_write_stamps_reaped_since` 等紅）

- [ ] **Step 3: 實作 write() override**

`src/models/agent_session.py`，緊接 `create()` 之後：

```python
    def write(self, vals):
        res = super().write(vals)
        new_state = vals.get('state')
        if new_state == 'reaped' and 'reaped_since' not in vals:
            stamp = self.filtered(lambda s: not s.reaped_since)
            if stamp:
                stamp.write({'reaped_since': fields.Datetime.now()})
        elif new_state and new_state != 'reaped':
            clear = self.filtered(lambda s: s.reaped_since)
            if clear:
                clear.write({'reaped_since': False})
        return res
```

- [ ] **Step 4: 跑測試確認 pass**

Run: TestReapedSinceInvariant 全綠。

- [ ] **Step 5: Commit**

```bash
git add src/models/agent_session.py src/tests/test_agent_reaped_since_invariant.py
git commit -m "fix: agent_session.write 強制 reaped_since invariant（drift/webhook reaped 也補值 → GC 可回收）"
```

---

## Task 2: 註冊測試 + Migration backfill 既有列

**Files:**
- Modify: `src/tests/__init__.py`（註冊新測試）
- Create: `src/migrations/18.0.1.0.40/post-migrate.py`
- Test: `src/tests/test_agent_reaped_since_invariant.py`（加 migration helper 測試）

- [ ] **Step 1: 註冊測試**

`src/tests/__init__.py` 加：

```python
from . import test_agent_reaped_since_invariant
```

- [ ] **Step 2: 寫 migration failing test**

在 `TestReapedSinceInvariant` 加：

```python
    def test_migration_backfills_reaped_null(self):
        from odoo.addons.woow_paas_platform.migrations import _backfill_reaped_since
        # 用 raw SQL 造一筆 reaped + reaped_since=NULL（繞過 write override）
        self.session.write({'state': 'reaped'})
        self.env.cr.execute(
            "UPDATE woow_paas_platform_agent_session "
            "SET reaped_since = NULL WHERE id = %s", (self.session.id,))
        self.session.invalidate_recordset(['reaped_since'])
        self.assertFalse(self.session.reaped_since)
        _backfill_reaped_since(self.env.cr)
        self.session.invalidate_recordset(['reaped_since'])
        self.assertTrue(self.session.reaped_since)
```

> 註：把 backfill SQL 抽成 `migrations/__init__.py` 的 `_backfill_reaped_since(cr)` helper，讓 post-migrate 與單元測試共用同一段邏輯（migration 檔本身不易在 TransactionCase 直接觸發）。

- [ ] **Step 3: 跑測試確認 fail**

Expected: FAIL（`_backfill_reaped_since` 不存在 → ImportError）

- [ ] **Step 4: 實作 backfill helper + post-migrate**

`src/migrations/__init__.py`（若不存在則建）加：

```python
def _backfill_reaped_since(cr):
    """state='reaped' 但 reaped_since 為 NULL 的列 → 補 now（給 T_gc 寬限）。冪等。"""
    cr.execute("""
        UPDATE woow_paas_platform_agent_session
        SET reaped_since = (now() AT TIME ZONE 'UTC')
        WHERE state = 'reaped' AND reaped_since IS NULL
    """)
```

`src/migrations/18.0.1.0.40/post-migrate.py`（新建）：

```python
from odoo.addons.woow_paas_platform.migrations import _backfill_reaped_since


def migrate(cr, version):
    _backfill_reaped_since(cr)
```

- [ ] **Step 5: 跑測試確認 pass**

Run: `test_migration_backfills_reaped_null` 綠 + 整檔綠。

- [ ] **Step 6: Commit**

```bash
git add src/tests/__init__.py src/migrations/
git commit -m "fix: migration backfill 既有 reaped+NULL 列的 reaped_since（GC 可回收）"
```

---

## Task 3: Manifest 版號 bump

**Files:**
- Modify: `src/__manifest__.py`

- [ ] **Step 1: bump version**

`18.0.1.0.39` → `18.0.1.0.40`。

- [ ] **Step 2: 跑全模組套件確認無回歸**

Run（沙盒）：`test --module woow_paas_platform`，Expected: `0 failed, 0 error(s)`（特別確認既有 `TestGcReaped` / `TestIdleReaper` 不因 write override 而紅）。

- [ ] **Step 3: Commit**

```bash
git add src/__manifest__.py
git commit -m "release: 18.0.1.0.40"
```

---

## Self-Review

- **Spec coverage**：write override（stamp-on-enter ✓ / clear-on-leave ✓ / 不覆蓋 ✓）、migration backfill ✓、版號 ✓、測試 1-7 全對應 task。
- **Placeholder scan**：無 TBD。
- **Type consistency**：`_backfill_reaped_since(cr)` 在 helper 與 post-migrate、測試三處同名同簽名。
- **回歸風險**：write override 改變 `write({'state':'reaped'})` 行為 → 既有 `TestGcReaped.setUp` 會自動補 reaped_since，但測試隨後 `_set_reaped_age` 覆寫，且 `test_skips_null_reaped_since` 走 `write({'reaped_since':False})`（無 state，override 不干涉）→ 預期不破；Task 3 Step 2 全套驗證守住。
