# Agent Long-term GC cron（D3）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 長期 `reaped` 的 agent session 超過 T_gc（預設 30 天）→ 完整 teardown（複用 `_teardown_runtime`）→ `absent`，補齊 `active → reaped → absent` 生命週期。

**Architecture:** Odoo cron（每天）掃 `state='reaped' AND reaped_since < now - T_gc` → `session._teardown_runtime()`（D2 既有，做 helm uninstall + delete ns）包 try/except（best-effort）→ 成功設 `state='absent'`。沿用 D4 的 Odoo-cron 自包含路線。

**Tech Stack:** Odoo 18（Python 4-space）、ir.cron、`ir.config_parameter`、複用 `agent_session._teardown_runtime`；測試 `TransactionCase` + `unittest.mock`（本機 k8s-dev-sandbox）。

**Spec:** `docs/archived/superpowers/specs/2026-06-14-agent-gc-cron-design.md`

---

## File Structure

- Modify: `src/models/agent_session.py` — 新增 `_cron_gc_reaped()`（放在 `_cron_idle_reaper` 之後、`_teardown_runtime` 之前/附近皆可；它 runtime 呼叫 `_teardown_runtime`，定義順序不拘）。
- Modify: `src/data/agent_session_cron.xml` — 在 idle reaper cron 之後新增 `ir_cron_agent_session_gc`。
- Modify: `src/hooks.py:142`（`_CONFIG_PARAMETERS`，`agent_idle_reap_seconds` 那行之後）— 加 `agent_gc_seconds`。
- Create: `src/tests/test_agent_gc_cron.py` + register in `src/tests/__init__.py`。

既有可複用（已驗證）：`_teardown_runtime()`（`agent_session.py:294`，做 client.delete → 完整 teardown，失敗 raise `AgentRuntimeOperatorError`）、`fields.Datetime.now()`、`timedelta`（已 import）、`get_agent_runtime_client(env)`、session 欄位 `state/reaped_since/project_namespace_hash`。

---

## Task 1: GC cron `_cron_gc_reaped`（核心）

**Files:**
- Modify: `src/models/agent_session.py`（`_cron_idle_reaper` 之後）
- Test: `src/tests/test_agent_gc_cron.py`（新建）+ `src/tests/__init__.py`

- [ ] **Step 1: 寫失敗測試（並在 __init__.py 註冊，否則 RED 為 0 tests 非真 red）**

```python
# src/tests/test_agent_gc_cron.py
import os
from datetime import timedelta
from unittest.mock import MagicMock, patch

from odoo import fields
from odoo.tests.common import TransactionCase, tagged

from odoo.addons.woow_paas_platform.services.agent_runtime_client import (
    AgentRuntimeOperatorError,
)

_ENV_NAMESPACE_SECRET = 'AGENT_RUNTIME_NAMESPACE_SECRET'
_CLIENT_PATH = (
    "odoo.addons.woow_paas_platform.services."
    "agent_runtime_client.get_agent_runtime_client"
)


@tagged('post_install', '-at_install')
class TestGcReaped(TransactionCase):
    def setUp(self):
        super().setUp()
        os.environ[_ENV_NAMESPACE_SECRET] = 'unit-test-secret-0123456789'
        self.addCleanup(os.environ.pop, _ENV_NAMESPACE_SECRET, None)
        self.Session = self.env['woow_paas_platform.agent_session'].sudo()
        self.ICP = self.env['ir.config_parameter'].sudo()
        self.ICP.set_param('woow_paas_platform.agent_gc_seconds', '2592000')
        self.project = self.env['project.project'].sudo().create({'name': 'GC'})
        self.session = self.Session.create({'project_id': self.project.id})
        self.session.write({'state': 'reaped'})
        self.hash = self.session.project_namespace_hash

    def _set_reaped_age(self, seconds):
        self.session.write({
            'reaped_since': fields.Datetime.now() - timedelta(seconds=seconds)})

    def test_gc_expired_reaped(self):
        self._set_reaped_age(31 * 24 * 3600)  # 31 天 > 30
        client = MagicMock()
        with patch(_CLIENT_PATH, return_value=client):
            self.Session._cron_gc_reaped()
        client.delete.assert_called_once_with(self.hash)
        self.assertEqual(self.session.state, 'absent')

    def test_skips_fresh_reaped(self):
        self._set_reaped_age(24 * 3600)  # 1 天 < 30
        client = MagicMock()
        with patch(_CLIENT_PATH, return_value=client):
            self.Session._cron_gc_reaped()
        client.delete.assert_not_called()
        self.assertEqual(self.session.state, 'reaped')

    def test_skips_non_reaped(self):
        self.session.write({'state': 'active'})
        self._set_reaped_age(31 * 24 * 3600)
        client = MagicMock()
        with patch(_CLIENT_PATH, return_value=client):
            self.Session._cron_gc_reaped()
        client.delete.assert_not_called()

    def test_skips_null_reaped_since(self):
        self.session.write({'reaped_since': False})
        client = MagicMock()
        with patch(_CLIENT_PATH, return_value=client):
            self.Session._cron_gc_reaped()
        client.delete.assert_not_called()
        self.assertEqual(self.session.state, 'reaped')

    def test_best_effort_on_teardown_failure(self):
        self._set_reaped_age(31 * 24 * 3600)
        client = MagicMock()
        client.delete.side_effect = AgentRuntimeOperatorError("operator 5xx")
        with patch(_CLIENT_PATH, return_value=client):
            # 不可拋例外（cron 不能整批炸）
            self.Session._cron_gc_reaped()
        self.assertEqual(self.session.state, 'reaped')  # 留 reaped 隔天重試

    def test_t_gc_config_respected(self):
        self._set_reaped_age(2 * 3600)  # 2h
        self.ICP.set_param('woow_paas_platform.agent_gc_seconds', '3600')  # 1h
        client = MagicMock()
        with patch(_CLIENT_PATH, return_value=client):
            self.Session._cron_gc_reaped()
        client.delete.assert_called_once()  # 2h > 1h → GC
        self.assertEqual(self.session.state, 'absent')
```

在 `src/tests/__init__.py` 末尾加：`from . import test_agent_gc_cron`

- [ ] **Step 2: 跑測試確認失敗**

Run（沙盒）：`kubectl --context docker-desktop exec -n dev-sandbox-feat-agent-life-cycle-cron deploy/feat-agent-life-cycle-cron-odoo-dev-sandbox-odoo -- odoo -u woow_paas_platform --test-enable --http-port=8999 --gevent-port=8995 --test-tags "/woow_paas_platform:TestGcReaped" --stop-after-init`
Expected: FAIL（`_cron_gc_reaped` 不存在 → 6 error）

- [ ] **Step 3: 最小實作**

```python
# src/models/agent_session.py — class AgentSession 內，_cron_idle_reaper 之後
    @api.model
    def _cron_gc_reaped(self):
        """Long-term GC（design §2.5 / D3）：reaped 超過 T_gc → 完整 teardown
        （helm uninstall + delete namespace，複用 _teardown_runtime）→ absent。

        best-effort：teardown 失敗 → log + skip，留 reaped 隔天重試（不阻塞、不
        raise；一筆失敗不影響同批其他）。reaped pod 已 scale-to-0，延後回收無孤兒
        風險（與 D2 unlink 的 fail-block 不同，與 D4 reaper 一致）。
        T_gc = ir.config_parameter agent_gc_seconds（預設 2592000/30天）。
        reaped_since 為空 → skip（防禦）。operator 未設定 → 整批短路。
        """
        from ..services.agent_runtime_client import get_agent_runtime_client
        if not get_agent_runtime_client(self.env):
            return
        seconds = int(self.env['ir.config_parameter'].sudo().get_param(
            'woow_paas_platform.agent_gc_seconds', 2592000))
        threshold = fields.Datetime.now() - timedelta(seconds=seconds)
        for session in self.sudo().search([('state', '=', 'reaped')]):
            if not session.reaped_since or session.reaped_since >= threshold:
                continue
            try:
                session._teardown_runtime()
            except Exception as exc:
                _logger.warning(
                    'GC teardown failed for agent_session %s: %s',
                    session.id, exc)
                continue
            session.write({'state': 'absent'})
```

- [ ] **Step 4: 跑測試確認通過**

Run: 同 Step 2
Expected: PASS（6 tests）

- [ ] **Step 5: Commit**

```bash
git add src/models/agent_session.py src/tests/test_agent_gc_cron.py src/tests/__init__.py
git commit -m "feat: agent_session._cron_gc_reaped 長期 GC（reaped 超 T_gc → 完整 teardown → absent，best-effort）"
```

---

## Task 2: Wiring — GC cron record + config 註冊

**Files:**
- Modify: `src/data/agent_session_cron.xml`（idle reaper cron 之後）
- Modify: `src/hooks.py:142`（`_CONFIG_PARAMETERS`）

- [ ] **Step 1: 加 GC cron record**

在 `src/data/agent_session_cron.xml` 的 `<data noupdate="1">` 內，idle reaper cron（`ir_cron_agent_session_idle_reaper`）之後加：

```xml
        <!-- Long-term GC (design §2.5 / D3): reaped sessions whose
             reaped-since exceeds T_gc → full teardown → absent. Daily. -->
        <record id="ir_cron_agent_session_gc" model="ir.cron">
            <field name="name">Agent Session: Long-term GC</field>
            <field name="model_id" ref="model_woow_paas_platform_agent_session"/>
            <field name="state">code</field>
            <field name="code">model._cron_gc_reaped()</field>
            <field name="interval_number">1</field>
            <field name="interval_type">days</field>
            <field name="active" eval="True"/>
        </record>
```

- [ ] **Step 2: 註冊 config_parameter 卸載清理**

`src/hooks.py` 的 `_CONFIG_PARAMETERS`，在 `'woow_paas_platform.agent_idle_reap_seconds',`（line 140）之後加一行：

```python
    'woow_paas_platform.agent_gc_seconds',
```

- [ ] **Step 3: 升級模組、驗證 cron 載入 + 全套件回歸**

Run（沙盒）：
```bash
kubectl --context docker-desktop exec -n dev-sandbox-feat-agent-life-cycle-cron deploy/feat-agent-life-cycle-cron-odoo-dev-sandbox-odoo -- odoo -u woow_paas_platform --test-enable --http-port=8999 --gevent-port=8995 --test-tags "/woow_paas_platform" --stop-after-init
```
Expected:
- `0 failed, 0 error(s)` 全套件綠。
- cron record 驗證：`kubectl --context docker-desktop exec -n dev-sandbox-feat-agent-life-cycle-cron statefulset/feat-agent-life-cycle-cron-odoo-dev-sandbox-postgres -- psql -U odoo -d woow_feat_agent_life_cycle_cron -tAc "SELECT cron_name||'|'||interval_number||'|'||interval_type FROM ir_cron WHERE cron_name='Agent Session: Long-term GC';"` → 回 `Agent Session: Long-term GC|1|days`。

- [ ] **Step 4: Commit**

```bash
git add src/data/agent_session_cron.xml src/hooks.py
git commit -m "feat: 接 GC cron — ir.cron(每天) + agent_gc_seconds config 註冊"
```

---

## Self-Review

**1. Spec coverage：**
- §① GC cron（每天、掃 reaped + reaped_since<threshold、複用 _teardown_runtime、best-effort、成功→absent、reaped_since 空→skip、operator 未設定→短路）→ Task 1 ✓
- §② config（agent_gc_seconds=2592000 + hooks 清理）→ Task 1（讀取）+ Task 2 Step 2（hooks 註冊）✓
- cron record（每天、noupdate）→ Task 2 Step 1 ✓
- error handling 對照（成功 absent / 失敗留 reaped / reaped_since 空 skip）→ Task 1 測試 test_gc_expired_reaped / test_best_effort_on_teardown_failure / test_skips_null_reaped_since ✓
- 生命週期補齊 active→reaped→absent → reaper（D4，已 merged）+ 本 GC ✓

**2. Placeholder scan：** 無 TBD/TODO；每個 code step 均含完整程式碼；cron 驗證給了具體 SQL。

**3. Type consistency：** `_cron_gc_reaped()`（無參、`@api.model`）、`_teardown_runtime()`（無參、複用）、`AgentRuntimeOperatorError`（測試 import）、config key `woow_paas_platform.agent_gc_seconds`、cron id `ir_cron_agent_session_gc`、name `Agent Session: Long-term GC` —— 全 plan 一致。`timedelta`/`fields` 已於 agent_session.py import。
