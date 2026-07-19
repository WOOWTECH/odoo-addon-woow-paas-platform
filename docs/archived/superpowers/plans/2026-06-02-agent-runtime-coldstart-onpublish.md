---
title: "agent-runtime cold-start retry (#123) + skill/mcp on-publish push (#111) Implementation Plan"
category: archived
status: archived
created: 2026-06-02
archived: 2026-07-19
archived-from: docs/superpowers/plans/2026-06-02-agent-runtime-coldstart-onpublish.md
archived-reason: "#123/#111 已隨 PR #130 落地，retry/timeout 分級與 on-publish 推送機制的現行權威為 docs/guides/development/agent-runtime-chat-architecture.md §1.4/§4.5，收尾狀態由 follow-ups.md F-ARL-1/F-ARC-1 承載"
---

# agent-runtime cold-start retry (#123) + skill/mcp on-publish push (#111) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 讓 agent-runtime cold-start 首則訊息自動退避重連（不再手動重試），並讓 skill/mcp 發布時主動推送 snapshot 給 running pod。

**Architecture:** 純 Odoo 端。先把 snapshot/resolve 邏輯從 controller 下沉到 `agent_session` model（chat 與 publish 共用一份），再各別補 #123 的 client timeout 分級 + spawn retry + WS connect retry + 前端進度，以及 #111 的 `invalidate_skills` + best-effort on-publish 推送。operator / chart / image 完全不動。

**Tech Stack:** Odoo 18（Python 3.11）、OWL（JS）、`requests`、`websockets`、Odoo `TransactionCase` / `HttpCase`。

**設計依據：** `docs/archived/superpowers/specs/2026-06-02-agent-runtime-coldstart-onpublish-design.md`

**全域慣例（每個 commit 適用）：**
- commit message 用英文、格式 `{type}: {message}`、**不加** AI 生成標記（user CLAUDE.md）。
- 測試 class 加 `@tagged('post_install', '-at_install')`。
- 跑單一測試：`./odoo-bin -c odoo.conf --test-enable --test-tags /woow_paas_platform --stop-after-init`（或在 `k8s-dev-sandbox` 跑，依 CLAUDE.md）。本機無 odoo-bin 時，於 sandbox：`./.claude/skills/k8s-dev-sandbox/scripts/test.sh <sandbox> --module woow_paas_platform`。

---

## File Structure

| 檔案 | 角色 | 動作 |
| --- | --- | --- |
| `src/models/agent_session.py` | snapshot/resolve 下沉的單一來源 + on-publish 推送協調 | Modify |
| `src/controllers/ai_assistant.py` | SSE bridge；snapshot/resolve 改 delegate；relay WS connect retry + status event | Modify |
| `src/services/agent_runtime_client.py` | per-call timeout、spawn retry、新增 `invalidate_skills` | Modify |
| `src/models/skill.py` | `write` state 變動 → 觸發推送 | Modify |
| `src/models/mcp_server.py` | `write` snapshot 相關欄位變動 → 觸發推送 | Modify |
| `src/static/src/paas/components/ai-chat/AiChat.js` | 新增 `status`/`spawning` 進度顯示 | Modify |
| `src/tests/test_agent_runtime_snapshot_refactor.py` | Task 1 回歸 | Create |
| `src/tests/test_agent_runtime_coldstart_retry.py` | Task 2-4 #123 | Create |
| `src/tests/test_agent_runtime_onpublish_push.py` | Task 6-9 #111 | Create |

**介面契約（跨 task 一致）：**
- `agent_session._platform_default_agent_template()` → template recordset（`@api.model`）
- `agent_session._resolve_agent_template(project)` → template recordset（`@api.model`）
- `agent_session._build_mcp_snapshot(project, template)` → `{'servers': {...}}`（`@api.model`）
- `agent_session._build_skill_snapshot(project, template)` → `[{name,version,sha256,description}]`（`@api.model`）
- `agent_session._push_snapshot_to_running_pods(kind, scope, project_id=None)` → None（`@api.model`，`kind ∈ {'skill','mcp'}`）
- `AgentRuntimeClient._request(method, path, json_body=None, timeout=None)`
- `AgentRuntimeClient.invalidate_skills(project_hash, skill_snapshot)` → dict
- 前端 `connectionState` 新增 `'spawning'`

---

## Phase 0 — 共用 refactor（snapshot/resolve 下沉）

### Task 1: 把 snapshot/resolve 下沉到 `agent_session`，controller 改 delegate

**Files:**
- Modify: `src/models/agent_session.py`（新增 4 個 `@api.model` 方法）
- Modify: `src/controllers/ai_assistant.py:17-60`（module function delegate）、`:831-901`（class method delegate）
- Test: `src/tests/test_agent_runtime_snapshot_refactor.py`

- [ ] **Step 1: 寫 failing test（model 上有共用方法且輸出與舊 controller 等價）**

Create `src/tests/test_agent_runtime_snapshot_refactor.py`:

```python
"""Refactor regression: snapshot/resolve 下沉到 agent_session model 後，
model 版輸出與 controller delegate 一致（spec Refactor①）。"""
import uuid

from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestSnapshotRefactor(TransactionCase):
    def setUp(self):
        super().setUp()
        self.Session = self.env['woow_paas_platform.agent_session'].sudo()
        self.project = self.env['project.project'].sudo().create(
            {'name': f'snap-{uuid.uuid4().hex[:6]}'})

    def _make_template(self, **kw):
        vals = {'name': f'tmpl-{uuid.uuid4().hex[:6]}'}
        vals.update(kw)
        return self.env['woow_paas_platform.agent_template'].sudo().create(vals)

    def test_skill_snapshot_shape(self):
        """system published skill 出現在 snapshot；draft 不出現。"""
        Skill = self.env['woow_paas_platform.skill'].sudo()
        Skill.create({'name': f'sk-{uuid.uuid4().hex[:6]}', 'version': '1.0.0',
                      'scope': 'system', 'state': 'published',
                      'bundle_sha256': 'a' * 64})
        template = self._make_template()
        snap = self.Session._build_skill_snapshot(self.project, template)
        self.assertIsInstance(snap, list)
        self.assertTrue(all('name' in s and 'sha256' in s for s in snap))

    def test_mcp_snapshot_shape(self):
        template = self._make_template()
        snap = self.Session._build_mcp_snapshot(self.project, template)
        self.assertIn('servers', snap)
        self.assertIsInstance(snap['servers'], dict)

    def test_resolve_returns_recordset(self):
        template = self._make_template(is_public=True, state='published',
                                       project_ids=[(6, 0, [self.project.id])])
        resolved = self.Session._resolve_agent_template(self.project)
        self.assertEqual(resolved.id, template.id)
```

- [ ] **Step 2: 跑測試確認 fail**

Run（sandbox 或 odoo-bin）：`--test-tags /woow_paas_platform` 篩 `TestSnapshotRefactor`
Expected: FAIL — `AttributeError: ... '_build_skill_snapshot'`（model 尚無此方法）。

- [ ] **Step 3: 在 `agent_session.py` 新增 4 個 `@api.model` 方法**

在 `src/models/agent_session.py` 的 class 內（`_cron_drift_detection` 之後）新增。把 controller `ai_assistant.py:17-52`（`_platform_default_agent_template`）、`:831-901`（resolve + 兩個 builder）的邏輯搬進來，`request.env` / `env` → `self.env`：

```python
    # ----------------------------------------------------------------- #
    # Snapshot / resolve — single source shared by chat (controller) and #
    # on-publish push (#111). Down-sunk from ai_assistant.py (spec ①).   #
    # ----------------------------------------------------------------- #
    @api.model
    def _platform_default_agent_template(self):
        """Platform default template if valid, else empty recordset.

        Valid = config_parameter set + exists + is_public + published +
        project-unbound (cross-tenant MCP/skill leak guard)."""
        AgentTemplate = self.env['woow_paas_platform.agent_template'].sudo()
        default_id = self.env['ir.config_parameter'].sudo().get_param(
            'woow_paas_platform.default_agent_template_id')
        if not default_id:
            return AgentTemplate.browse()
        try:
            candidate = AgentTemplate.browse(int(default_id)).exists()
        except (ValueError, TypeError):
            return AgentTemplate.browse()
        if candidate and candidate.is_public \
                and candidate.state == 'published' \
                and not candidate.project_ids:
            return candidate
        return AgentTemplate.browse()

    @api.model
    def _resolve_agent_template(self, project):
        """project-bound published → project-bound any → project.agent_template_id
        → platform default. Last fallback so a project's own binding wins."""
        AgentTemplate = self.env['woow_paas_platform.agent_template'].sudo()
        template = AgentTemplate.search([
            ('project_ids', 'in', project.id),
            ('state', '=', 'published'),
        ], limit=1)
        if not template:
            template = AgentTemplate.search([
                ('project_ids', 'in', project.id),
            ], limit=1)
        if not template and project.agent_template_id:
            template = project.agent_template_id
        if not template:
            template = self._platform_default_agent_template()
        return template or AgentTemplate.browse()

    @api.model
    def _build_mcp_snapshot(self, project, template):
        """Serialise the template's bound MCP servers (name → url/transport)."""
        servers = {}
        for mcp in template.mcp_server_ids:
            servers[mcp.name] = {'url': mcp.url or '', 'transport': mcp.transport}
        return {'servers': servers}

    @api.model
    def _build_skill_snapshot(self, project, template):
        """Serialise the template's published skills + system skills for the pod
        (name/version/sha256/description; system shadowed by same-name bound)."""
        Skill = self.env['woow_paas_platform.skill'].sudo()
        bound = template.skill_ids.filtered(lambda s: s.state == 'published')
        bound_names = set(bound.mapped('name'))
        system = Skill.search([
            ('scope', '=', 'system'),
            ('state', '=', 'published'),
            ('name', 'not in', list(bound_names)),
        ])
        return [
            {'name': skill.name, 'version': skill.version,
             'sha256': skill.bundle_sha256,
             'description': skill.description or ''}
            for skill in (bound + system)
        ]
```

確認 `agent_session.py` 頂部已 `from odoo import api, fields, models`（已有，line 8）。

- [ ] **Step 4: controller 改 delegate（行為等價）**

在 `src/controllers/ai_assistant.py`：

把 module function `_platform_default_agent_template`（line 17-52 整段）替換為 thin wrapper（保留簽名，既有呼叫者 line 60/83/856 零改動）：

```python
def _platform_default_agent_template(env):
    """Delegate to the single source on agent_session (spec Refactor①)."""
    return env['woow_paas_platform.agent_session']._platform_default_agent_template()
```

把 class 方法 `_resolve_agent_template`（line 831-857）、`_build_mcp_snapshot`（867-872）、`_build_skill_snapshot`（874-901）整段 body 替換為 delegate：

```python
    def _resolve_agent_template(self, project):
        return request.env['woow_paas_platform.agent_session'] \
            ._resolve_agent_template(project)

    def _build_mcp_snapshot(self, project, template):
        return request.env['woow_paas_platform.agent_session'] \
            ._build_mcp_snapshot(project, template)

    def _build_skill_snapshot(self, project, template):
        return request.env['woow_paas_platform.agent_session'] \
            ._build_skill_snapshot(project, template)
```

- [ ] **Step 5: 跑新測試 + 既有 agent-runtime 回歸**

Run: 篩 `TestSnapshotRefactor` + 既有 `test_agent_pod_phase_c_sse_gateway`、`test_default_agent_template`、`test_auto_enable_agent_runtime`、`test_mcp_chat_consumption`。
Expected: PASS（新測試綠 + 既有全綠，證明 delegate 等價）。

- [ ] **Step 6: Commit**

```bash
git add src/models/agent_session.py src/controllers/ai_assistant.py src/tests/test_agent_runtime_snapshot_refactor.py
git commit -m "refactor: down-sink agent-runtime snapshot/resolve to agent_session model"
```

---

## Phase 1 — #123 cold-start retry

### Task 2: client `_request` per-call timeout（spawn 90s / invalidate 10s）

**Files:**
- Modify: `src/services/agent_runtime_client.py:84-97`（`_request` 加 `timeout`）、`:110-132`（spawn 傳 90）、`:140-144`（invalidate 傳 10）
- Test: `src/tests/test_agent_runtime_coldstart_retry.py`

- [ ] **Step 1: 寫 failing test**

Create `src/tests/test_agent_runtime_coldstart_retry.py`:

```python
"""#123 cold-start retry: client timeout 分級 + spawn retry + WS connect retry."""
import os
import uuid
from unittest.mock import MagicMock, patch

from odoo.tests.common import TransactionCase, tagged

_CLIENT_MOD = "odoo.addons.woow_paas_platform.services.agent_runtime_client"
_ENV_NAMESPACE_SECRET = 'AGENT_RUNTIME_NAMESPACE_SECRET'


@tagged('post_install', '-at_install')
class TestColdStartClientTimeout(TransactionCase):
    def setUp(self):
        super().setUp()
        ICP = self.env['ir.config_parameter'].sudo()
        ICP.set_param('woow_paas_platform.operator_url', 'http://operator.test:8000')
        ICP.set_param('woow_paas_platform.operator_api_key', 'test-api-key')
        os.environ[_ENV_NAMESPACE_SECRET] = 'unit-test-secret-0123456789'
        self.addCleanup(os.environ.pop, _ENV_NAMESPACE_SECRET, None)
        import importlib
        self.mod = importlib.import_module(_CLIENT_MOD)
        self.client = self.mod.get_agent_runtime_client(self.env)

    def _ok(self, payload=None):
        resp = MagicMock()
        resp.status_code = 200
        resp.ok = True
        resp.json.return_value = payload or {}
        return resp

    def test_spawn_uses_90s_timeout(self):
        with patch.object(self.mod, 'requests', create=True) as mock_requests:
            mock_requests.request.return_value = self._ok(
                {'pod_ws_url': 'ws://pod', 'auth_token': 't',
                 'pod_state': 'active', 'spawn_age_ms': 10})
            project = self.env['project.project'].sudo().create(
                {'name': f'to-{uuid.uuid4().hex[:6]}'})
            self.client.spawn_or_attach(
                project=project, channel_id=1, invoker_user_id=2,
                invoker_role='user',
                agent_template=MagicMock(id=1, model_config_id=None),
                mcp_snapshot={}, skill_snapshot=[])
            self.assertEqual(mock_requests.request.call_args.kwargs.get('timeout'), 90)

    def test_invalidate_uses_10s_timeout(self):
        with patch.object(self.mod, 'requests', create=True) as mock_requests:
            mock_requests.request.return_value = self._ok({})
            self.client.invalidate_mcp('pa1234567890abcd', {})
            self.assertEqual(mock_requests.request.call_args.kwargs.get('timeout'), 10)

    def test_reap_keeps_default_30s_timeout(self):
        with patch.object(self.mod, 'requests', create=True) as mock_requests:
            mock_requests.request.return_value = self._ok({})
            self.client.reap('pa1234567890abcd')
            self.assertEqual(mock_requests.request.call_args.kwargs.get('timeout'), 30)
```

- [ ] **Step 2: 跑測試確認 fail**

Run: 篩 `TestColdStartClientTimeout`
Expected: FAIL — spawn timeout 目前是 30（固定 `_TIMEOUT`），不是 90。

- [ ] **Step 3: 實作 per-call timeout**

在 `src/services/agent_runtime_client.py`：

`_request` 簽名加 `timeout`，None 時用 `_TIMEOUT`：

```python
    def _request(self, method, path, json_body=None, timeout=None):
        url = self.base_url + _PREFIX + path
        resp = requests.request(
            method, url,
            json=json_body,
            headers={'X-API-Key': self.api_key, 'Content-Type': 'application/json'},
            timeout=timeout if timeout is not None else _TIMEOUT,
        )
        if not resp.ok:
            raise AgentRuntimeOperatorError(
                'paas-operator %s %s → %s: %s'
                % (method, path, resp.status_code, self._error_detail(resp))
            )
        return resp.json()
```

頂部常數區（line 16 附近）加：

```python
_TIMEOUT = 30
_SPAWN_TIMEOUT = 90      # cold-start helm install + image pull headroom (#123)
_INVALIDATE_TIMEOUT = 10  # best-effort on-publish push, don't hang (#123/#111)
```

`spawn_or_attach` 的 return（line 132）改：

```python
        return self._request('POST', '/spawn-or-attach', body,
                             timeout=_SPAWN_TIMEOUT)
```

`invalidate_mcp`（line 140-144）改：

```python
    def invalidate_mcp(self, project_hash, mcp_snapshot=None):
        return self._request(
            'POST', f'/{project_hash}/invalidate-mcp',
            {'mcp_snapshot': mcp_snapshot or {}},
            timeout=_INVALIDATE_TIMEOUT,
        )
```

- [ ] **Step 4: 跑測試確認 pass**

Run: 篩 `TestColdStartClientTimeout` + 既有 `test_agent_pod_phase_c_client_webhook`（CL.*）
Expected: PASS（新 3 綠 + 既有 CL.* 不破，因為 default timeout 行為對 reap/status 不變）。

- [ ] **Step 5: Commit**

```bash
git add src/services/agent_runtime_client.py src/tests/test_agent_runtime_coldstart_retry.py
git commit -m "feat: per-call timeout for agent-runtime client (spawn 90s, invalidate 10s)"
```

---

### Task 3: client `spawn_or_attach` bounded retry（idempotent，逾時/連線類）

**Files:**
- Modify: `src/services/agent_runtime_client.py`（import exceptions、spawn retry loop）
- Test: `src/tests/test_agent_runtime_coldstart_retry.py`（同檔加 class）

- [ ] **Step 1: 寫 failing test**

在 `test_agent_runtime_coldstart_retry.py` 末尾加：

```python
@tagged('post_install', '-at_install')
class TestSpawnRetry(TransactionCase):
    def setUp(self):
        super().setUp()
        ICP = self.env['ir.config_parameter'].sudo()
        ICP.set_param('woow_paas_platform.operator_url', 'http://operator.test:8000')
        ICP.set_param('woow_paas_platform.operator_api_key', 'test-api-key')
        os.environ[_ENV_NAMESPACE_SECRET] = 'unit-test-secret-0123456789'
        self.addCleanup(os.environ.pop, _ENV_NAMESPACE_SECRET, None)
        import importlib
        self.mod = importlib.import_module(_CLIENT_MOD)
        self.client = self.mod.get_agent_runtime_client(self.env)

    def _ok(self):
        resp = MagicMock()
        resp.status_code = 200
        resp.ok = True
        resp.json.return_value = {'pod_ws_url': 'ws://pod', 'auth_token': 't',
                                  'pod_state': 'active', 'spawn_age_ms': 10}
        return resp

    def test_spawn_retries_on_timeout_then_succeeds(self):
        import requests as real_requests
        with patch.object(self.mod, 'requests', create=True) as mock_requests, \
                patch.object(self.mod.time, 'sleep', return_value=None):
            mock_requests.request.side_effect = [
                real_requests.exceptions.ReadTimeout('t1'),
                real_requests.exceptions.ReadTimeout('t2'),
                self._ok(),
            ]
            project = self.env['project.project'].sudo().create(
                {'name': f'rt-{uuid.uuid4().hex[:6]}'})
            result = self.client.spawn_or_attach(
                project=project, channel_id=1, invoker_user_id=2,
                invoker_role='user',
                agent_template=MagicMock(id=1, model_config_id=None),
                mcp_snapshot={}, skill_snapshot=[])
            self.assertEqual(result['pod_ws_url'], 'ws://pod')
            self.assertEqual(mock_requests.request.call_count, 3)

    def test_spawn_does_not_retry_on_4xx(self):
        with patch.object(self.mod, 'requests', create=True) as mock_requests, \
                patch.object(self.mod.time, 'sleep', return_value=None):
            bad = MagicMock()
            bad.status_code = 400
            bad.ok = False
            bad.json.return_value = {'detail': 'bad request'}
            bad.text = 'bad request'
            mock_requests.request.return_value = bad
            project = self.env['project.project'].sudo().create(
                {'name': f'b4-{uuid.uuid4().hex[:6]}'})
            with self.assertRaises(self.mod.AgentRuntimeOperatorError):
                self.client.spawn_or_attach(
                    project=project, channel_id=1, invoker_user_id=2,
                    invoker_role='user',
                    agent_template=MagicMock(id=1, model_config_id=None),
                    mcp_snapshot={}, skill_snapshot=[])
            self.assertEqual(mock_requests.request.call_count, 1)  # no retry on 4xx
```

- [ ] **Step 2: 跑測試確認 fail**

Run: 篩 `TestSpawnRetry`
Expected: FAIL — 目前無 retry（first ReadTimeout 直接拋）；且 `self.mod.time` 不存在。

- [ ] **Step 3: 實作 spawn retry**

在 `src/services/agent_runtime_client.py` 頂部 import 區加：

```python
import time
from requests import exceptions as _req_exc
```

常數區加：

```python
_SPAWN_MAX_RETRIES = 2          # total attempts = 1 + 2 (#123 cold-start)
_SPAWN_RETRY_BACKOFF = 2.0      # seconds, linear between attempts
```

`spawn_or_attach` 的最後一行（`return self._request(...)`）改成 retry loop。把 body 組裝保留，最後改：

```python
        # F-ARL-1 / #123: cold-start spawn can exceed even the 90s headroom or
        # hit a transient connection error while helm install + image pull are
        # in flight. spawn-or-attach is IDEMPOTENT (operator _release_exists →
        # attach), so retrying transient timeout / connection errors is safe.
        # 4xx/5xx (AgentRuntimeOperatorError) are NOT retried — surface them.
        last_exc = None
        for attempt in range(_SPAWN_MAX_RETRIES + 1):
            try:
                return self._request('POST', '/spawn-or-attach', body,
                                     timeout=_SPAWN_TIMEOUT)
            except (_req_exc.Timeout, _req_exc.ConnectionError) as exc:
                last_exc = exc
                if attempt < _SPAWN_MAX_RETRIES:
                    _logger.warning(
                        'spawn-or-attach transient error (attempt %s/%s): %s',
                        attempt + 1, _SPAWN_MAX_RETRIES + 1, exc)
                    time.sleep(_SPAWN_RETRY_BACKOFF)
                    continue
                raise
        raise last_exc  # unreachable, defensive
```

- [ ] **Step 4: 跑測試確認 pass**

Run: 篩 `TestSpawnRetry` + `TestColdStartClientTimeout` + 既有 CL.*
Expected: PASS（retry 後成功 call_count=3；4xx 不 retry call_count=1）。

- [ ] **Step 5: Commit**

```bash
git add src/services/agent_runtime_client.py src/tests/test_agent_runtime_coldstart_retry.py
git commit -m "feat: bounded retry for idempotent spawn-or-attach on transient errors (#123)"
```

---

### Task 4: controller relay WS connect retry + `status:spawning` 進度 event

**Files:**
- Modify: `src/controllers/ai_assistant.py:723-752`（`relay()` connect 改 retry，保留 `__aenter__/__aexit__` 清理語意）
- Test: `src/tests/test_agent_runtime_coldstart_retry.py`（HttpCase）

- [ ] **Step 1: 寫 failing test（HttpCase，fake ws 前 N 次 refused）**

在 `test_agent_runtime_coldstart_retry.py` 末尾加（fake ws 範本取自 `test_agent_pod_phase_d_sse_aclose.py`，新增 `fail_times`）：

```python
import json as _json
import re
import sys
from odoo.tests.common import HttpCase


class _RetryFakeWSConn:
    def __init__(self, frames):
        self._frames = list(frames)
        self.closed = False
        self.sent = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        import asyncio
        await asyncio.sleep(0)
        self.closed = True
        return False

    async def send(self, message):
        self.sent.append(message)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._frames:
            return self._frames.pop(0)
        raise StopAsyncIteration


def _make_retry_ws(fail_times, conn):
    import types
    mod = types.ModuleType("websockets")
    mod._attempts = 0

    def connect(uri, *args, **kwargs):
        mod._attempts += 1
        if mod._attempts <= fail_times:
            raise ConnectionRefusedError("pod not ready (simulated)")
        return conn

    mod.connect = connect
    return mod


@tagged('post_install', '-at_install')
class TestWsConnectRetry(HttpCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.admin = cls.env.ref('base.user_admin')

    def setUp(self):
        super().setUp()
        s = uuid.uuid4().hex[:8]
        Project = self.env['project.project'].sudo()
        self.project = Project.create({'name': f'wsr-{s}'})
        self.project.message_subscribe(partner_ids=[self.admin.partner_id.id])
        self.project.write({'use_agent_runtime': True})
        self.env['woow_paas_platform.agent_template'].sudo().create({
            'name': f'tmpl-{s}', 'is_public': True, 'state': 'published',
            'project_ids': [(6, 0, [self.project.id])]})
        self.channel = self.env['discuss.channel'].sudo().create({
            'name': f'wsr-ch-{s}', 'channel_type': 'channel',
            'channel_partner_ids': [(4, self.admin.partner_id.id)]})
        self.env['project.task'].sudo().create({
            'name': f'wsr-task-{s}', 'project_id': self.project.id,
            'channel_id': self.channel.id})

    def _csrf_token(self):
        for page in ('/woow', '/web'):
            resp = self.url_open(page, timeout=15)
            if resp.status_code != 200:
                continue
            m = re.search(r'csrf_token[\"\']?\s*[:=]\s*[\"\']([^\"\']+)[\"\']',
                          resp.text)
            if m:
                return m.group(1)
        self.fail("csrf token not found (fixture issue)")

    def _fake_client(self):
        client = MagicMock()
        client.spawn_or_attach.return_value = {
            'pod_ws_url': 'ws://pod.local:8000/v1/chat',
            'auth_token': 'tok', 'pod_state': 'spawning', 'spawn_age_ms': 5}
        return client

    def test_connect_retries_then_streams_done(self):
        conn = _RetryFakeWSConn(frames=[
            _json.dumps({'type': 'text_chunk', 'payload': {'content': 'hi'}}),
            _json.dumps({'type': 'done', 'payload': {}}),
        ])
        fake = _make_retry_ws(fail_times=2, conn=conn)
        prev = sys.modules.get('websockets')
        sys.modules['websockets'] = fake
        self.addCleanup(lambda: sys.modules.__setitem__('websockets', prev)
                        if prev else sys.modules.pop('websockets', None))
        self.authenticate('admin', 'admin')
        token = self._csrf_token()
        url = f'/api/ai/stream/{self.channel.id}?csrf_token={token}'
        with patch(_CLIENT_MOD + '.get_agent_runtime_client',
                   return_value=self._fake_client()):
            resp = self.url_open(url, timeout=25)
        self.assertEqual(resp.status_code, 200)
        self.assertIn('"stage": "spawning"', resp.text)   # 進度 event 有出
        self.assertIn('"done": true', resp.text)          # 最終仍成功
        self.assertEqual(fake._attempts, 3)               # 2 fail + 1 success
        self.assertTrue(conn.sent)                         # user_message 有送
        self.assertEqual(len(conn.sent), 1)               # 只送一次（不重送）
```

- [ ] **Step 2: 跑測試確認 fail**

Run: 篩 `TestWsConnectRetry`
Expected: FAIL — 現況 connect 第一次 `ConnectionRefusedError` 直接拋到 SSE error，無 `stage:spawning`、無 done。

- [ ] **Step 3: 改 `relay()` 為 connect retry（保留清理語意）**

把 `src/controllers/ai_assistant.py:723-752` 的 `relay()` 整段替換為（retry 只包 connect/enter；連線成功才 send + drain；finally 走 `__aexit__` 保 F-PC-6 清理）：

```python
            async def relay():
                # F-ARL-1 / #123 G3: pod may not be WS-ready right after
                # spawn-or-attach returns pod_state="spawning". Retry only the
                # CONNECT (not send/drain) with backoff up to a deadline, so a
                # mid-stream disconnect never re-sends user_message. Keep the
                # __aenter__/__aexit__ pair so the F-PC-6 aclose() cleanup still
                # closes the socket on the done/error break.
                cm = None
                ws = None
                backoff = 1.0
                start = loop.time()
                deadline = start + 60
                while ws is None:
                    try:
                        cm = websockets.connect(
                            pod_ws_url,
                            extra_headers={'Authorization': f'Bearer {auth_token}'},
                            ping_interval=30,
                            ping_timeout=60,
                        )
                        ws = await cm.__aenter__()
                    except (ConnectionRefusedError, OSError, asyncio.TimeoutError):
                        cm = None
                        if loop.time() >= deadline:
                            yield {'type': 'error', 'payload': {
                                'error': 'agent 啟動逾時，請稍後再試',
                                'fatal': True}}
                            return
                        yield {'type': 'status', 'payload': {
                            'stage': 'spawning',
                            'elapsed_ms': int((loop.time() - start) * 1000)}}
                        await asyncio.sleep(backoff)
                        backoff = min(backoff * 2, 5)
                try:
                    await ws.send(json.dumps({
                        'version': '1.0',
                        'type': 'user_message',
                        'request_id': request_id,
                        'sequence_number': 0,
                        'timestamp': _dt.utcnow().isoformat() + 'Z',
                        'payload': {
                            'channel_id': channel_id,
                            'body': user_message,
                            'history': history,
                            'template_id': template_id,
                            'invoker_role': invoker_role,
                            'invoker_user_id': uid,
                        },
                    }))
                    async for raw in ws:
                        envelope = json.loads(raw)
                        yield envelope
                        if envelope.get('type') == 'done':
                            return
                        if envelope.get('type') == 'error' and \
                                envelope.get('payload', {}).get('fatal'):
                            return
                finally:
                    await cm.__aexit__(None, None, None)
```

注意：`asyncio` 已在 `generate()` 頂部 import（line 712）；`relay()` 是 `generate()` 的內嵌函式，可見 `asyncio` / `_dt` / `websockets`。`loop` 也在 `relay` 外層可見（閉包）。

- [ ] **Step 4: 跑測試確認 pass + 既有 aclose 回歸**

Run: 篩 `TestWsConnectRetry` + 既有 `test_agent_pod_phase_d_sse_aclose`（AC.6a/b/c）
Expected: PASS — 新測試綠；aclose 測試仍綠（`__aexit__` 在 finally 照常被呼叫，`conn.closed=True`）。

- [ ] **Step 5: Commit**

```bash
git add src/controllers/ai_assistant.py src/tests/test_agent_runtime_coldstart_retry.py
git commit -m "feat: bounded WS connect retry with spawning progress event (#123 G3)"
```

---

### Task 5: 前端 `status:spawning` 進度顯示

**Files:**
- Modify: `src/static/src/paas/components/ai-chat/AiChat.js:58`（state 註解）、`:204-272`（onmessage 加 status 分支）、`:740-769`（connectionIndicator 加 spawning）
- Test: 手動 / e2e（OWL 無單元測試框架；行為由 Task 4 後端測試 + 視覺驗證守）

- [ ] **Step 1: onmessage 加 `status` 分支**

在 `AiChat.js` 的 `onmessage`，於 `if (data.type === "tool_call")` 之前（line 216 前）插入：

```javascript
        // #123 cold-start: pod 啟動中的進度 event（非錯誤）
        if (data.type === "status" && data.payload?.stage === "spawning") {
          this._reconnectAttempts = 0;
          this._consecutiveParseErrors = 0;
          this.state.connectionState = "spawning";
          this.state.error = null;
          return;
        }
```

- [ ] **Step 2: connectionIndicator 加 `spawning` 狀態**

在 `AiChat.js:740` 的 `connectionIndicator` 的 `map` 內，`connecting` 之後加：

```javascript
      spawning: {
        color: "yellow",
        text: "AI 啟動中…（首次啟動需數十秒）",
        visible: true,
        animate: "blink",
      },
```

並把 line 58 的狀態註解補上 `spawning`：

```javascript
      connectionState: "idle", // idle | connecting | spawning | connected | streaming | error | reconnecting
```

- [ ] **Step 3: 手動驗證（sandbox）**

部署到 sandbox（`deploy-local` 或 `k8s-dev-sandbox`），於 `/woow` 對一個 cold pod 的 project 送首則訊息：應看到黃色「AI 啟動中…」而非紅色錯誤，pod ready 後轉為「AI 回覆中…」。
> 若暫時無法起 cold pod，至少 `grep` 確認 `spawning` 已加入 indicator map 與 onmessage 分支。

- [ ] **Step 4: Commit**

```bash
git add src/static/src/paas/components/ai-chat/AiChat.js
git commit -m "feat: show spawning progress indicator during cold-start (#123)"
```

---

## Phase 2 — #111 skill/mcp on-publish push

### Task 6: client `invalidate_skills`

**Files:**
- Modify: `src/services/agent_runtime_client.py`（`invalidate_mcp` 之後加 `invalidate_skills`）
- Test: `src/tests/test_agent_runtime_onpublish_push.py`

- [ ] **Step 1: 寫 failing test**

Create `src/tests/test_agent_runtime_onpublish_push.py`:

```python
"""#111 on-publish push: client invalidate_skills + agent_session 推送協調 +
skill/mcp 觸發 hook（best-effort）。"""
import os
import uuid
from unittest.mock import MagicMock, patch

from odoo.tests.common import TransactionCase, tagged

_CLIENT_MOD = "odoo.addons.woow_paas_platform.services.agent_runtime_client"
_ENV_NAMESPACE_SECRET = 'AGENT_RUNTIME_NAMESPACE_SECRET'


@tagged('post_install', '-at_install')
class TestInvalidateSkillsClient(TransactionCase):
    def setUp(self):
        super().setUp()
        ICP = self.env['ir.config_parameter'].sudo()
        ICP.set_param('woow_paas_platform.operator_url', 'http://operator.test:8000')
        ICP.set_param('woow_paas_platform.operator_api_key', 'test-api-key')
        import importlib
        self.mod = importlib.import_module(_CLIENT_MOD)
        self.client = self.mod.get_agent_runtime_client(self.env)

    def test_invalidate_skills_posts_list(self):
        with patch.object(self.mod, 'requests', create=True) as mock_requests:
            resp = MagicMock()
            resp.status_code = 200
            resp.ok = True
            resp.json.return_value = {'patched': True}
            mock_requests.request.return_value = resp
            snap = [{'name': 'sk', 'version': '1.0.0', 'sha256': 'a' * 64,
                     'description': ''}]
            self.client.invalidate_skills('pa1234567890abcd', snap)
            call = mock_requests.request.call_args
            import json as _j
            blob = _j.dumps([call.args, call.kwargs], default=str)
            self.assertIn('pa1234567890abcd/invalidate-skills', blob)
            self.assertEqual(call.kwargs.get('json'), {'skill_snapshot': snap})
            self.assertEqual(call.kwargs.get('timeout'), 10)  # short timeout
```

- [ ] **Step 2: 跑測試確認 fail**

Run: 篩 `TestInvalidateSkillsClient`
Expected: FAIL — `AttributeError: ... 'invalidate_skills'`。

- [ ] **Step 3: 實作 `invalidate_skills`**

在 `src/services/agent_runtime_client.py` 的 `invalidate_mcp` 之後加：

```python
    def invalidate_skills(self, project_hash, skill_snapshot=None):
        return self._request(
            'POST', f'/{project_hash}/invalidate-skills',
            {'skill_snapshot': skill_snapshot or []},
            timeout=_INVALIDATE_TIMEOUT,
        )
```

- [ ] **Step 4: 跑測試確認 pass**

Run: 篩 `TestInvalidateSkillsClient`
Expected: PASS。

- [ ] **Step 5: Commit**

```bash
git add src/services/agent_runtime_client.py src/tests/test_agent_runtime_onpublish_push.py
git commit -m "feat: add invalidate_skills to agent-runtime client (#111)"
```

---

### Task 7: `agent_session._push_snapshot_to_running_pods`（best-effort 協調）

**Files:**
- Modify: `src/models/agent_session.py`（新增推送方法）
- Test: `src/tests/test_agent_runtime_onpublish_push.py`（同檔加 class）

- [ ] **Step 1: 寫 failing test**

在 `test_agent_runtime_onpublish_push.py` 末尾加：

```python
@tagged('post_install', '-at_install')
class TestPushCoordination(TransactionCase):
    def setUp(self):
        super().setUp()
        os.environ[_ENV_NAMESPACE_SECRET] = 'unit-test-secret-0123456789'
        self.addCleanup(os.environ.pop, _ENV_NAMESPACE_SECRET, None)
        ICP = self.env['ir.config_parameter'].sudo()
        ICP.set_param('woow_paas_platform.operator_url', 'http://operator.test:8000')
        ICP.set_param('woow_paas_platform.operator_api_key', 'test-api-key')
        self.Session = self.env['woow_paas_platform.agent_session'].sudo()

    def _active_session(self):
        project = self.env['project.project'].sudo().create(
            {'name': f'ps-{uuid.uuid4().hex[:6]}', 'use_agent_runtime': True})
        session = self.Session.create({'project_id': project.id})
        session.write({'state': 'active'})
        return project, session

    def test_system_scope_pushes_all_active(self):
        self._active_session()
        self._active_session()
        fake = MagicMock()
        with patch(_CLIENT_MOD + '.get_agent_runtime_client', return_value=fake):
            self.Session._push_snapshot_to_running_pods('skill', 'system')
        self.assertEqual(fake.invalidate_skills.call_count, 2)

    def test_user_scope_pushes_only_that_project(self):
        p1, _ = self._active_session()
        self._active_session()
        fake = MagicMock()
        with patch(_CLIENT_MOD + '.get_agent_runtime_client', return_value=fake):
            self.Session._push_snapshot_to_running_pods('skill', 'user', p1.id)
        self.assertEqual(fake.invalidate_skills.call_count, 1)

    def test_no_client_is_noop(self):
        self._active_session()
        with patch(_CLIENT_MOD + '.get_agent_runtime_client', return_value=None):
            # must not raise
            self.Session._push_snapshot_to_running_pods('skill', 'system')

    def test_single_pod_failure_does_not_abort(self):
        self._active_session()
        self._active_session()
        fake = MagicMock()
        fake.invalidate_skills.side_effect = [Exception('boom'), {'ok': True}]
        with patch(_CLIENT_MOD + '.get_agent_runtime_client', return_value=fake):
            self.Session._push_snapshot_to_running_pods('skill', 'system')
        self.assertEqual(fake.invalidate_skills.call_count, 2)  # 第一個失敗不中斷

    def test_reaped_session_not_pushed(self):
        project = self.env['project.project'].sudo().create(
            {'name': f'rp-{uuid.uuid4().hex[:6]}', 'use_agent_runtime': True})
        session = self.Session.create({'project_id': project.id})
        session.write({'state': 'reaped'})
        fake = MagicMock()
        with patch(_CLIENT_MOD + '.get_agent_runtime_client', return_value=fake):
            self.Session._push_snapshot_to_running_pods('skill', 'system')
        self.assertEqual(fake.invalidate_skills.call_count, 0)  # 只推 active
```

- [ ] **Step 2: 跑測試確認 fail**

Run: 篩 `TestPushCoordination`
Expected: FAIL — `AttributeError: ... '_push_snapshot_to_running_pods'`。

- [ ] **Step 3: 實作推送方法**

在 `src/models/agent_session.py` 的 Task 1 新增方法之後加（沿用 `_cron_drift_detection` 的 best-effort 範式：client=None 略過、逐筆 try/except warning continue）：

```python
    @api.model
    def _push_snapshot_to_running_pods(self, kind, scope, project_id=None):
        """#111: best-effort on-publish push of a fresh snapshot to running pods.

        kind ∈ {'skill','mcp'}. system scope → all active sessions (cross
        project); user scope → only that project's session. Only state='active'
        pods (a 'spawning' cold-start carries the latest snapshot already;
        'reaped'/'absent' have no pod). Single-pod failure is logged and
        skipped — never aborts the publish (issue #111 明訂 best-effort);
        next chat's attach refresh is the fallback. ConfigMap kubelet
        propagation means "instant" is best-effort.
        """
        from ..services.agent_runtime_client import get_agent_runtime_client
        client = get_agent_runtime_client(self.env)
        if not client:
            return
        domain = [('state', '=', 'active')]
        if scope == 'user':
            if not project_id:
                return
            domain.append(('project_id', '=', project_id))
        sessions = self.sudo().search(domain)
        for session in sessions:
            project = session.project_id
            if not project or not project.use_agent_runtime:
                continue
            project_hash = session.project_namespace_hash
            if not project_hash:
                continue
            template = self._resolve_agent_template(project)
            if not template:
                continue
            try:
                if kind == 'skill':
                    snapshot = self._build_skill_snapshot(project, template)
                    client.invalidate_skills(project_hash, snapshot)
                else:
                    snapshot = self._build_mcp_snapshot(project, template)
                    client.invalidate_mcp(project_hash, snapshot)
            except Exception as exc:
                _logger.warning(
                    'on-publish %s push failed for agent_session %s: %s',
                    kind, session.id, exc)
                continue
```

- [ ] **Step 4: 跑測試確認 pass**

Run: 篩 `TestPushCoordination`
Expected: PASS（system=2、user=1、no-client no-op、單 pod 失敗仍跑完 2 次、reaped=0）。

- [ ] **Step 5: Commit**

```bash
git add src/models/agent_session.py src/tests/test_agent_runtime_onpublish_push.py
git commit -m "feat: agent_session best-effort on-publish snapshot push coordinator (#111)"
```

---

### Task 8: skill `write` state 變動 → 觸發 skill 推送

**Files:**
- Modify: `src/models/skill.py:175-180`（state 分支末尾加推送）
- Test: `src/tests/test_agent_runtime_onpublish_push.py`（同檔加 class）

- [ ] **Step 1: 寫 failing test**

在 `test_agent_runtime_onpublish_push.py` 末尾加：

```python
@tagged('post_install', '-at_install')
class TestSkillPublishTriggersPush(TransactionCase):
    def setUp(self):
        super().setUp()
        os.environ[_ENV_NAMESPACE_SECRET] = 'unit-test-secret-0123456789'
        self.addCleanup(os.environ.pop, _ENV_NAMESPACE_SECRET, None)
        ICP = self.env['ir.config_parameter'].sudo()
        ICP.set_param('woow_paas_platform.operator_url', 'http://operator.test:8000')
        ICP.set_param('woow_paas_platform.operator_api_key', 'test-api-key')

    def test_system_skill_publish_calls_push(self):
        skill = self.env['woow_paas_platform.skill'].sudo().create({
            'name': f'sk-{uuid.uuid4().hex[:6]}', 'version': '1.0.0',
            'scope': 'system', 'state': 'draft', 'bundle_sha256': 'a' * 64})
        Session = self.env['woow_paas_platform.agent_session']
        with patch.object(type(Session), '_push_snapshot_to_running_pods') as push:
            skill.action_publish()
            push.assert_called()
            # scope='system' 傳遞正確
            args = push.call_args.args
            self.assertEqual(args[0], 'skill')
            self.assertEqual(args[1], 'system')

    def test_recompute_is_latest_does_not_trigger_push(self):
        """_recompute_is_latest 內部 write 的是 is_latest（不含 state）→ 不推送，
        不遞迴。"""
        skill = self.env['woow_paas_platform.skill'].sudo().create({
            'name': f'sk-{uuid.uuid4().hex[:6]}', 'version': '1.0.0',
            'scope': 'system', 'state': 'published', 'bundle_sha256': 'a' * 64})
        Session = self.env['woow_paas_platform.agent_session']
        with patch.object(type(Session), '_push_snapshot_to_running_pods') as push:
            skill._recompute_is_latest()
            push.assert_not_called()
```

- [ ] **Step 2: 跑測試確認 fail**

Run: 篩 `TestSkillPublishTriggersPush`
Expected: FAIL — `action_publish` 目前不呼叫推送。

- [ ] **Step 3: 在 skill `write` 的 state 分支加推送**

在 `src/models/skill.py` 的 `write`（line 175-180），把 state 分支改為（loop 結束後對每筆 skill 推送，only state-changing write 才進這裡）：

```python
        if 'state' in vals:
            for skill in self:
                if skill.state == 'published' and not skill.published_at:
                    skill.published_at = fields.Datetime.now()
                skill._recompute_is_latest()
                # #111: state change (publish/deprecate/archive) alters which
                # skills load into running pods → push fresh snapshot best-
                # effort. _recompute_is_latest writes only is_latest (no
                # 'state') so it never re-enters this branch (no recursion).
                self.env['woow_paas_platform.agent_session'] \
                    ._push_snapshot_to_running_pods(
                        'skill', skill.scope, skill.project_id.id or None)
```

- [ ] **Step 4: 跑測試確認 pass + skill 既有回歸**

Run: 篩 `TestSkillPublishTriggersPush` + 既有 `test_skill_admin_lifecycle`、`test_skill_is_latest_drift_regression`、`test_skill_dual_scope`
Expected: PASS — 推送被呼叫、`_recompute_is_latest` 不觸發；既有 skill 生命週期測試不破（push 在無 operator 設定的測試裡是 no-op）。

> 注意：若既有 skill 測試 setUp **未**設 operator → `_push...` client=None no-op，不影響。若有設且無 mock，會嘗試真連 → 但測試 env 連不到 operator.test、`invalidate_skills` raise → 被 best-effort 吞掉，publish 仍成功（行為正確）。

- [ ] **Step 5: Commit**

```bash
git add src/models/skill.py src/tests/test_agent_runtime_onpublish_push.py
git commit -m "feat: push skill snapshot to running pods on publish/deprecate (#111)"
```

---

### Task 9: mcp_server `write` snapshot 相關欄位變動 → 觸發 mcp 推送

**Files:**
- Modify: `src/models/mcp_server.py:134-175`（`write` 末尾加推送）
- Test: `src/tests/test_agent_runtime_onpublish_push.py`（同檔加 class）

- [ ] **Step 1: 寫 failing test**

在 `test_agent_runtime_onpublish_push.py` 末尾加：

```python
@tagged('post_install', '-at_install')
class TestMcpChangeTriggersPush(TransactionCase):
    def setUp(self):
        super().setUp()
        os.environ[_ENV_NAMESPACE_SECRET] = 'unit-test-secret-0123456789'
        self.addCleanup(os.environ.pop, _ENV_NAMESPACE_SECRET, None)
        ICP = self.env['ir.config_parameter'].sudo()
        ICP.set_param('woow_paas_platform.operator_url', 'http://operator.test:8000')
        ICP.set_param('woow_paas_platform.operator_api_key', 'test-api-key')

    def _mcp(self, **kw):
        vals = {'name': f'mcp-{uuid.uuid4().hex[:6]}', 'scope': 'system',
                'transport': 'sse', 'url': 'https://a.example/sse'}
        vals.update(kw)
        return self.env['woow_paas_platform.mcp_server'].sudo().create(vals)

    def test_url_change_calls_mcp_push(self):
        server = self._mcp()
        Session = self.env['woow_paas_platform.agent_session']
        with patch.object(type(Session), '_push_snapshot_to_running_pods') as push:
            server.write({'url': 'https://b.example/sse'})
            push.assert_called()
            args = push.call_args.args
            self.assertEqual(args[0], 'mcp')
            self.assertEqual(args[1], 'system')

    def test_non_snapshot_field_change_no_push(self):
        """state_message 不在 snapshot（snapshot 只有 name/url/transport）→ 不推。"""
        server = self._mcp()
        Session = self.env['woow_paas_platform.agent_session']
        with patch.object(type(Session), '_push_snapshot_to_running_pods') as push:
            server.write({'state_message': 'hello'})
            push.assert_not_called()
```

- [ ] **Step 2: 跑測試確認 fail**

Run: 篩 `TestMcpChangeTriggersPush`
Expected: FAIL — `mcp_server.write` 目前不推送。

- [ ] **Step 3: 在 mcp_server `write` 末尾加條件推送**

先確認現有 `write` 的結尾（`src/models/mcp_server.py:134-175`）有 `result = super().write(vals)` 與 `return result`。在 `return result` 之前插入：

```python
        # #111 parity: mcp snapshot 只序列化 server 的 name/url/transport
        # (_build_mcp_snapshot)，故僅這些欄位變動才改 pod 看到的 mcp.json →
        # 觸發 best-effort 推送。active/state/state_message/tool 變動不改 snapshot
        # 故不推（mcp_tool 同理不觸發）。
        _MCP_SNAPSHOT_FIELDS = {'name', 'url', 'transport'}
        if _MCP_SNAPSHOT_FIELDS & set(vals):
            Session = self.env['woow_paas_platform.agent_session']
            for server in self:
                Session._push_snapshot_to_running_pods(
                    'mcp', server.scope, server.project_id.id or None)
```

> 若 `write` body 內已有早 return 分支，務必把上述插在「所有路徑都會經過的 `return result` 前」；若有多個 return，抽成一次。檢視 `mcp_server.py:134-175` 後放在最終 return 前。

- [ ] **Step 4: 跑測試確認 pass + mcp 既有回歸**

Run: 篩 `TestMcpChangeTriggersPush` + 既有 `test_mcp_scope_flip_guard`、`test_mcp_integration`、`test_mcp_chat_consumption`、`test_mcp_sidecar_backed_lifecycle`
Expected: PASS — url 變更推送、state_message 不推；既有 mcp 測試不破（best-effort no-op / 吞錯）。

- [ ] **Step 5: Commit**

```bash
git add src/models/mcp_server.py src/tests/test_agent_runtime_onpublish_push.py
git commit -m "feat: push mcp snapshot to running pods on server url/transport change (#111)"
```

---

## Phase 3 — docs 更新

### Task 10: 更新 follow-ups / pitfall / chat-architecture

**Files:**
- Modify: `docs/tracking/followups/follow-ups.md`（F-ARL-1 標 RESOLVED + #111 收尾）
- Modify: `docs/tracking/pitfalls/agent-runtime-fpilot10-cluster-e2e-gap-chain.md`（G3 OPEN→CLOSED）
- Modify: `docs/guides/development/agent-runtime-chat-architecture.md`（§1.4 補 WS retry；新增 on-publish 推送小節）

- [ ] **Step 1: follow-ups.md**

把 F-ARL-1（line 45 區段）的狀態從 OPEN 改為 RESOLVED，補一行指向本 PR + plan：
`F-ARL-1（#123）RESOLVED：client timeout 分級(spawn 90s/invalidate 10s) + spawn idempotent retry + relay WS connect bounded retry(deadline 60s) + 前端 spawning 進度；plan docs/archived/superpowers/plans/2026-06-02-agent-runtime-coldstart-onpublish.md`。
另加 #111 收尾條目（invalidate_skills + agent_session on-publish push）。

- [ ] **Step 2: pitfall G3**

在 `agent-runtime-fpilot10-cluster-e2e-gap-chain.md` 的 G3 列：`OPEN` → `CLOSED`，修法欄補：`odoo relay WS connect bounded retry + backoff(deadline 60s) + status:spawning 進度 event；spawn idempotent retry + timeout 分級`。

- [ ] **Step 3: chat-architecture §1.4 + on-publish 小節**

- §1.4（`_stream_agent_runtime`）補：WS connect 現為 bounded retry（connect 失敗退避到 pod ready，deadline 60s；retry 只包 connect 不重送 user_message），期間推 `status:spawning`。
- 新增小節「skill/mcp on-publish 即時推送（#111）」：`skill.write`(state) / `mcp_server.write`(name/url/transport) → `agent_session._push_snapshot_to_running_pods` → 列舉 active session（system=全部/user=單一）→ `_build_*_snapshot` → `client.invalidate_skills/invalidate_mcp`（best-effort）。註明 mcp_tool 不改 snapshot 故不觸發。

- [ ] **Step 4: Commit**

```bash
git add docs/tracking/followups/follow-ups.md docs/tracking/pitfalls/agent-runtime-fpilot10-cluster-e2e-gap-chain.md docs/guides/development/agent-runtime-chat-architecture.md
git commit -m "docs: record agent-runtime cold-start retry (#123) and on-publish push (#111)"
```

---

## Final Verification

- [ ] 全模組測試綠：跑 `--test-tags /woow_paas_platform`（或 sandbox `test.sh`），確認本計畫新增 3 個測試檔 + 既有 agent-runtime / skill / mcp 測試全綠。
- [ ] grep 確認無殘留：`grep -rn "def _build_skill_snapshot\|def _build_mcp_snapshot" src/` 只剩 model 一份 body + controller delegate。
- [ ] 視覺驗證（sandbox）：cold-start 送訊息顯示「AI 啟動中…」、發布 system skill 時 running pod 即時收到（或 operator log 見 invalidate-skills 呼叫）。
- [ ] 在 gitea #123 / #111 回報結果（zh-tw），引用本 plan 與 PR。

---

## Self-Review 紀錄（writing-plans 要求）

- **Spec coverage**：Refactor①→Task 1；#123 timeout 分級→T2、spawn retry→T3、WS retry+進度→T4、前端→T5；#111 invalidate_skills→T6、推送協調→T7、skill 觸發→T8、mcp 觸發→T9；docs→T10。皆有對應 task。
- **與 spec 的一處刻意收斂**：spec 提「mcp_tool.write 觸發」，實作核對後 `_build_mcp_snapshot` 不含 tool（只 name/url/transport），故 mcp_tool 不觸發、改由 mcp_server 的 snapshot 欄位觸發（T9 已註明，避免無謂推送）。
- **型別一致**：`_build_skill_snapshot`→list、`_build_mcp_snapshot`→dict、`_push_snapshot_to_running_pods(kind, scope, project_id)`、`invalidate_skills(hash, list)`、`_request(...timeout=)`、前端 `connectionState='spawning'` 全 task 一致。
- **No placeholder**：每步含真實測試碼 / 實作碼 / 指令 / 期望輸出。
