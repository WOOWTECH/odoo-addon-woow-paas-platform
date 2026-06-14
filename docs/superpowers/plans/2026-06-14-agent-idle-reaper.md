# Agent Idle Reaper（D4）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 閒置的 agent runtime pod 自動 scale-to-0（`active → reaped`），釋放 cluster quota —— 使用者最初「agent 閒置兩天沒下線」問題的正解。

**Architecture:** Odoo 自包含（非 cluster CronJob）。Heartbeat 由 Odoo 在 SSE 串流開始 stamp `agent_session.last_active`；新 ir.cron 每 5 分鐘掃 `last_active` 過期的 `active` session → `client.reap()` → `state='reaped'`。reap 失敗 best-effort（留 active 下輪重試，與 D2 teardown 的 fail-block 相反）。

**Tech Stack:** Odoo 18（Python 4-space）、ir.cron、`ir.config_parameter`、既有 `AgentRuntimeClient.reap`；測試 `TransactionCase` + `unittest.mock`（本機 k8s-dev-sandbox 跑 `odoo --test-enable`）。

**Spec:** `docs/superpowers/specs/2026-06-14-agent-idle-reaper-design.md`

---

## File Structure

- Modify: `src/models/agent_session.py` — 新增 `_record_activity(project)`（heartbeat helper）+ `_cron_idle_reaper()`（reaper）+ 常數 `_ACTIVITY_THROTTLE_SECONDS`。
- Modify: `src/controllers/ai_assistant.py:~778` — `_stream_agent_runtime` 在 spawn_or_attach 成功後呼叫 `_record_activity`。
- Modify: `src/data/agent_session_cron.xml` — 新增 `ir_cron_agent_session_idle_reaper`。
- Modify: `src/hooks.py:142` — `_CONFIG_PARAMETERS` 加 `agent_idle_reap_seconds`。
- Create: `src/tests/test_agent_idle_reaper.py` + register in `src/tests/__init__.py`。

既有可複用（已驗證簽名）：`fields.Datetime.now()`、`from datetime import timedelta`（agent_session.py:8 已 import）、`get_agent_runtime_client(env)`→client|None、`client.reap(project_hash)`、`AgentRuntimeOperatorError`、session 欄位 `state/last_active/reaped_since/spawn_at/project_namespace_hash/project_id`。

---

## Task 1: Heartbeat helper `_record_activity`

**Files:**
- Modify: `src/models/agent_session.py`（class `AgentSession` 內，drift cron 之後）
- Test: `src/tests/test_agent_idle_reaper.py`（新建）+ `src/tests/__init__.py`

- [ ] **Step 1: 寫失敗測試**

```python
# src/tests/test_agent_idle_reaper.py
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
class TestRecordActivity(TransactionCase):
    def setUp(self):
        super().setUp()
        os.environ[_ENV_NAMESPACE_SECRET] = 'unit-test-secret-0123456789'
        self.addCleanup(os.environ.pop, _ENV_NAMESPACE_SECRET, None)
        self.Session = self.env['woow_paas_platform.agent_session'].sudo()
        self.project = self.env['project.project'].sudo().create({'name': 'HB'})
        self.session = self.Session.create({'project_id': self.project.id})

    def test_stamps_when_last_active_empty(self):
        self.assertFalse(self.session.last_active)
        self.Session._record_activity(self.project)
        self.assertTrue(self.session.last_active)

    def test_stamps_when_stale(self):
        self.session.write({
            'last_active': fields.Datetime.now() - timedelta(seconds=120)})
        before = self.session.last_active
        self.Session._record_activity(self.project)
        self.assertGreater(self.session.last_active, before)

    def test_skips_when_recent(self):
        recent = fields.Datetime.now() - timedelta(seconds=5)
        self.session.write({'last_active': recent})
        self.Session._record_activity(self.project)
        self.assertEqual(self.session.last_active, recent)

    def test_noop_when_no_session(self):
        proj2 = self.env['project.project'].sudo().create({'name': 'NoSess'})
        # 不應拋例外（該 project 無 session）
        self.Session._record_activity(proj2)
```

- [ ] **Step 2: 跑測試確認失敗**

Run（沙盒）：`odoo -u woow_paas_platform --test-enable --http-port=8999 --gevent-port=8995 --test-tags "/woow_paas_platform:TestRecordActivity" --stop-after-init`
Expected: FAIL（`_record_activity` 不存在 → AttributeError）

- [ ] **Step 3: 最小實作**

```python
# src/models/agent_session.py — class AgentSession 內（_cron_drift_detection 之後）
    _ACTIVITY_THROTTLE_SECONDS = 60

    @api.model
    def _record_activity(self, project):
        """Heartbeat：以 project 找其 session，stamp last_active（節流）。

        在 SSE 串流開始（spawn_or_attach 成功後、進 generator 前）呼叫。只在
        last_active 為空或舊於節流秒數才寫，避免寫入放大。session 不存在 → no-op。
        """
        session = self.sudo().search(
            [('project_id', '=', project.id)], limit=1)
        if not session:
            return
        now = fields.Datetime.now()
        if session.last_active and session.last_active > now - timedelta(
                seconds=self._ACTIVITY_THROTTLE_SECONDS):
            return
        session.write({'last_active': now})
```

並在 `src/tests/__init__.py` 加：`from . import test_agent_idle_reaper`

- [ ] **Step 4: 跑測試確認通過**

Run: 同 Step 2
Expected: PASS（4 tests）

- [ ] **Step 5: Commit**

```bash
git add src/models/agent_session.py src/tests/test_agent_idle_reaper.py src/tests/__init__.py
git commit -m "feat: agent_session._record_activity heartbeat helper（stamp last_active，節流 60s）"
```

---

## Task 2: Reaper cron `_cron_idle_reaper`（核心）

**Files:**
- Modify: `src/models/agent_session.py`（`_record_activity` 之後）
- Test: `src/tests/test_agent_idle_reaper.py`（追加 class）

- [ ] **Step 1: 寫失敗測試**

```python
# src/tests/test_agent_idle_reaper.py — 追加
@tagged('post_install', '-at_install')
class TestIdleReaper(TransactionCase):
    def setUp(self):
        super().setUp()
        os.environ[_ENV_NAMESPACE_SECRET] = 'unit-test-secret-0123456789'
        self.addCleanup(os.environ.pop, _ENV_NAMESPACE_SECRET, None)
        self.Session = self.env['woow_paas_platform.agent_session'].sudo()
        self.ICP = self.env['ir.config_parameter'].sudo()
        self.ICP.set_param('woow_paas_platform.agent_idle_reap_seconds', '1800')
        self.project = self.env['project.project'].sudo().create({'name': 'R'})
        self.session = self.Session.create({'project_id': self.project.id})
        self.session.write({'state': 'active'})

    def _set_idle(self, seconds):
        self.session.write({
            'last_active': fields.Datetime.now() - timedelta(seconds=seconds)})

    def test_reaps_expired_active(self):
        self._set_idle(3600)  # 1h > 30min
        client = MagicMock()
        with patch(_CLIENT_PATH, return_value=client):
            self.Session._cron_idle_reaper()
        client.reap.assert_called_once_with(self.session.project_namespace_hash)
        self.assertEqual(self.session.state, 'reaped')
        self.assertTrue(self.session.reaped_since)

    def test_skips_fresh_active(self):
        self._set_idle(60)  # 1min < 30min
        client = MagicMock()
        with patch(_CLIENT_PATH, return_value=client):
            self.Session._cron_idle_reaper()
        client.reap.assert_not_called()
        self.assertEqual(self.session.state, 'active')

    def test_skips_non_active(self):
        self.session.write({'state': 'reaped'})
        self._set_idle(3600)
        client = MagicMock()
        with patch(_CLIENT_PATH, return_value=client):
            self.Session._cron_idle_reaper()
        client.reap.assert_not_called()

    def test_best_effort_on_reap_failure(self):
        from odoo.addons.woow_paas_platform.services.agent_runtime_client \
            import AgentRuntimeOperatorError
        self._set_idle(3600)
        client = MagicMock()
        client.reap.side_effect = AgentRuntimeOperatorError("operator 5xx")
        with patch(_CLIENT_PATH, return_value=client):
            # 不可拋例外（cron 不能整批炸）
            self.Session._cron_idle_reaper()
        self.assertEqual(self.session.state, 'active')  # 留 active 下輪重試

    def test_null_last_active_falls_back_to_spawn_at(self):
        # 模擬 legacy：last_active 空、spawn_at 久遠
        self.session.write({
            'last_active': False,
            'spawn_at': fields.Datetime.now() - timedelta(seconds=3600)})
        client = MagicMock()
        with patch(_CLIENT_PATH, return_value=client):
            self.Session._cron_idle_reaper()
        client.reap.assert_called_once()
        self.assertEqual(self.session.state, 'reaped')

    def test_t_idle_config_respected(self):
        self._set_idle(120)  # 2min
        self.ICP.set_param('woow_paas_platform.agent_idle_reap_seconds', '60')
        client = MagicMock()
        with patch(_CLIENT_PATH, return_value=client):
            self.Session._cron_idle_reaper()
        client.reap.assert_called_once()  # 2min > 60s → reap
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `odoo -u woow_paas_platform --test-enable --http-port=8999 --gevent-port=8995 --test-tags "/woow_paas_platform:TestIdleReaper" --stop-after-init`
Expected: FAIL（`_cron_idle_reaper` 不存在）

- [ ] **Step 3: 最小實作**

```python
# src/models/agent_session.py — class AgentSession 內（_record_activity 之後）
    @api.model
    def _cron_idle_reaper(self):
        """掃描閒置 active session → reap（scale-to-0）。best-effort。

        判定：state='active' AND (last_active or spawn_at) < now - T_idle。
        T_idle = ir.config_parameter agent_idle_reap_seconds（預設 1800/30min）。
        reap 失敗（operator/網路）→ log + skip，留 active 下輪重試（不阻塞、不 raise；
        一筆失敗不影響同批其他 session）。operator reap 冪等。
        """
        from ..services.agent_runtime_client import get_agent_runtime_client
        client = get_agent_runtime_client(self.env)
        if not client:
            return
        seconds = int(self.env['ir.config_parameter'].sudo().get_param(
            'woow_paas_platform.agent_idle_reap_seconds', 1800))
        threshold = fields.Datetime.now() - timedelta(seconds=seconds)
        for session in self.sudo().search([('state', '=', 'active')]):
            effective = session.last_active or session.spawn_at
            if not effective or effective >= threshold:
                continue
            if not session.project_namespace_hash:
                continue
            try:
                client.reap(session.project_namespace_hash)
            except Exception as exc:
                _logger.warning(
                    'idle reap failed for agent_session %s: %s',
                    session.id, exc)
                continue
            session.write({
                'state': 'reaped',
                'reaped_since': fields.Datetime.now(),
            })
```

- [ ] **Step 4: 跑測試確認通過**

Run: 同 Step 2
Expected: PASS（6 tests）

- [ ] **Step 5: Commit**

```bash
git add src/models/agent_session.py src/tests/test_agent_idle_reaper.py
git commit -m "feat: agent_session._cron_idle_reaper 閒置 reap（best-effort，T_idle 可配置）"
```

---

## Task 3: Wiring — stream heartbeat + cron record + config 註冊

**Files:**
- Modify: `src/controllers/ai_assistant.py:~778`（`_stream_agent_runtime`，spawn_or_attach 成功後）
- Modify: `src/data/agent_session_cron.xml`
- Modify: `src/hooks.py:142`（`_CONFIG_PARAMETERS`）

- [ ] **Step 1: 接 heartbeat 進串流路徑**

在 `_stream_agent_runtime` 的 `spawn_or_attach` try/except 成功之後、`pod_ws_url = runtime_info.get(...)` 之前（約 line 778）插入：

```python
        # heartbeat：cursor 此處仍有效（generator 內無效，故在此 stamp）
        request.env['woow_paas_platform.agent_session']._record_activity(project)
```

- [ ] **Step 2: 加 reaper cron record**

在 `src/data/agent_session_cron.xml` 的 `<data noupdate="1">` 內，drift cron 之後加：

```xml
        <!-- Idle reaper (design §2.3 / D4): scan active sessions whose
             last_active is older than T_idle → reap (scale-to-0). -->
        <record id="ir_cron_agent_session_idle_reaper" model="ir.cron">
            <field name="name">Agent Session: Idle Reaper</field>
            <field name="model_id" ref="model_woow_paas_platform_agent_session"/>
            <field name="state">code</field>
            <field name="code">model._cron_idle_reaper()</field>
            <field name="interval_number">5</field>
            <field name="interval_type">minutes</field>
            <field name="active" eval="True"/>
        </record>
```

- [ ] **Step 3: 註冊 config_parameter 卸載清理**

`src/hooks.py` 的 `_CONFIG_PARAMETERS`（line 142 的 `]` 之前）加一行：

```python
    'woow_paas_platform.agent_idle_reap_seconds',
```

- [ ] **Step 4: 升級模組、驗證 cron 載入 + 全套件回歸**

Run（沙盒）：
```bash
odoo -u woow_paas_platform --test-enable --http-port=8999 --gevent-port=8995 \
  --test-tags "/woow_paas_platform" --stop-after-init
```
Expected:
- `0 failed, 0 error(s)` 全套件綠。
- 額外驗證 cron record：`SELECT cron_name FROM ir_cron WHERE cron_name='Agent Session: Idle Reaper';` 回一列（或 odoo-shell `env.ref('woow_paas_platform.ir_cron_agent_session_idle_reaper')` 存在）。

- [ ] **Step 5: Commit**

```bash
git add src/controllers/ai_assistant.py src/data/agent_session_cron.xml src/hooks.py
git commit -m "feat: 接 idle reaper — 串流 heartbeat + ir.cron(5min) + agent_idle_reap_seconds config"
```

---

## Self-Review

**1. Spec coverage：**
- §① heartbeat（`_record_activity`，串流開始 stamp、節流 60s）→ Task 1 + Task 3 Step 1 ✓
- §② reaper（scan active、(last_active or spawn_at)<threshold、client.reap、reaped+reaped_since、best-effort、冪等）→ Task 2 ✓
- §③ config（`agent_idle_reap_seconds`=1800 + hooks.py 清理）→ Task 2（讀取）+ Task 3 Step 3（hooks 註冊）✓
- cron record（5min、noupdate）→ Task 3 Step 2 ✓
- 防 re-reap loop（heartbeat 先於 state 同步）→ 由 Task 1 helper + Task 3 接線達成（last_active 串流開始即刷新）✓
- 測試（reap/skip/best-effort/null fallback/T_idle/heartbeat throttle）→ Task 1（4）+ Task 2（6）✓

**2. Placeholder scan：** 無 TBD/TODO；每個 code step 均含完整程式碼。Task 3 Step 4 的 cron 驗證給了 SQL 與 odoo-shell 兩種具體查法。

**3. Type consistency：** `_record_activity(project)`（model method、收 project record）、`_cron_idle_reaper()`（無參、`@api.model`）、`client.reap(project_hash)`、`AgentRuntimeOperatorError`、`_ACTIVITY_THROTTLE_SECONDS`=60、config key `woow_paas_platform.agent_idle_reap_seconds` —— 全 plan 一致。`timedelta`/`fields` 已於 agent_session.py import。
