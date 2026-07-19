---
title: "#271 內網 MCP bypass 防 regress 守衛 Implementation Plan"
category: plans
status: done
created: 2026-07-06
updated: 2026-07-19
source: docs/superpowers/plans/2026-07-06-271-internal-mcp-bypass-guard.md
promoted-at: 2026-07-19
---

# #271 內網 MCP bypass 防 regress 守衛 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 釘死「`sidecar_backed=True` 的 MCP url 必為 in-cluster DNS」invariant，擋掉內網 agent 資料面被 regress 成走公開 CF hostname（撞 CF Access 登入頁 + teardown 前綴漏收孤兒）。

**Architecture:** invariant = `sidecar_backed=True ⟹ url 為 in-cluster`（`urlparse(url).scheme == 'http'` 且 hostname 以 `.svc.cluster.local` 結尾）。以 **model `@api.constrains` 為主守衛**（對所有寫入含 sudo 生效、無 actor 豁免）＋ **controller `_validate_mcp_url` friendly error 為 defense-in-depth**（回 UI 友善字串、避免 legacy 端點 raw ValidationError leak），兩層共用**單一** `mcp_server._is_in_cluster_url()` classmethod 避免 lockstep 漂移。加 **migration healer**（`18.0.1.0.43`）把既有壞列 `sidecar_backed` 翻 False（承認它是外部 MCP，保留 url/tool_ids）。**前端 gating**：未部署服務（無 `helm_release_name`/`helm_namespace`）不顯示 Add Sidecar 按鈕，讓 sidecar flow 永遠只在已部署服務上開啟 → prefill 永遠回 in-cluster。刪除兩段 0-caller 的 public-first dead code。

**Tech Stack:** Odoo 18 addon（Python 4-space；ORM model `@api.constrains` / migration `post-migrate.py`）、OWL 前端（JS 2-space；純 getter 邏輯，本 repo 無 JS 測試框架 → headed 瀏覽器手動驗證）。測試用 Odoo `TransactionCase` / `HttpCase`（非 pytest）。

## Global Constraints

- **Invariant 精確定義（唯一真理，兩層必一致）**：`sidecar_backed == True` ⟹ `urlparse(url)` 的 `scheme == 'http'` 且 `(hostname or '').lower().endswith('.svc.cluster.local')`。`sidecar_backed == False` 不受此約束，仍可填任意 public https（維持外部 MCP 彈性）。**單向**——`in-cluster ⟹ sidecar_backed` 不成立（外部端點測試 `test_url_accepts_cluster_local` 用 cluster.local url 但 `sidecar_backed=False`，必須保持綠，勿 over-ban）。
- **共用 helper 避 two-layer 漂移**：in-cluster 判定只寫一份 = `mcp_server._is_in_cluster_url(url)` classmethod。model constrains 與 controller `_validate_mcp_url` 都呼叫它，禁止各寫一份（memory `two-layer-acl-lockstep`）。
- **constrains 不可用 su/group 豁免（hard invariant）**：新 `@api.constrains` **不得**寫 `not self.env.su` / `not has_group(...)`。它是「sidecar 就該 in-cluster、對 admin/sudo 一視同仁」的 hard invariant，鏡像既有 `_check_scope_project_consistency`（`mcp_server.py:118-132`）之無豁免寫法。若加 su 豁免，`TransactionCase`（su env）測試會跑不到 deny 分支 → **假綠**（memory `su-env-exempt-guard-false-green`）。與 scope-flip `write()` guard（`mcp_server.py:134-164`，那條**有** su 豁免）分野：scope 是可 gated 改的值，本條值對誰都不可為公開。
- **controller 不繞 model**：controller `_validate_mcp_url` 是 friendly-error 層，**不取代** model constrains；project-centric 端點既有 `try/except ValidationError` 保留（雙保險）。
- **每-task 綠 vs 全域綠**：Task 1 model constrains 一落地，既有 `test_mcp_sidecar_hint_endpoint.py` 的 `test_create_endpoint_does_not_enforce_url_consistency_backed_true_external`（斷言 sidecar_backed=True+external 會**成功**）會轉為 stale-red——這是**預期**，由 Task 6 正式反轉。故 **Task 1-5 只跑各自 targeted test 類**（保持該 task 綠），**全模組跑一律排到 Task 6 之後**。
- **測試註冊**：每個新 test 檔必須加進 `src/tests/__init__.py`，否則 Odoo 0 collected → 靜默假綠。`--test-tags` 必 slash-prefix `/woow_paas_platform:Class`（bare 名字被當 tag → 0 tests）。
- **commit message**：zh-tw `{prefix}: {message}`（prefix 英文小寫如 `feat:`/`fix:`/`refactor:`/`test:`），**無任何 AI 署名 / Co-Authored-By / Generated-with 行**。

---

### Task 1: Model constrains + 共用 in-cluster helper（設計 ①）

**Files:**
- Modify: `src/models/mcp_server.py`（import 區 `1-6`；helper + constrains 插在既有 `_check_scope_project_consistency` 結尾 `132` 與 `def write` `134` 之間）
- Create: `src/tests/test_mcp_sidecar_url_invariant.py`
- Modify: `src/tests/__init__.py`（註冊新 test 檔）

**Interfaces:**
- Consumes: 既有 `mcp_server` fields `url`（`Char`，`21-25`）、`sidecar_backed`（`Boolean`，`99-105`）；既有 `@api.constrains('scope','project_id')` 風格。
- Produces: `mcp_server._is_in_cluster_url(url: str) -> bool` classmethod（Task 2 controller 呼叫）；`@api.constrains('sidecar_backed','url')` `_check_sidecar_url_in_cluster`（對所有 create/write 生效）。

- [ ] **Step 1: Write the failing test**

建立 `src/tests/test_mcp_sidecar_url_invariant.py`：

```python
"""TDD — #271 mcp_server sidecar-backed url in-cluster invariant.

Model-layer @api.constrains + shared _is_in_cluster_url helper.
Design: docs/plans/2026-Q3/2026-07-06-271-internal-mcp-bypass-guard-design.md §4 ①

Red expectation (pre-impl):
  * _is_in_cluster_url helper does not exist yet → AttributeError.
  * No @api.constrains → creating sidecar_backed=True + public url succeeds
    → assertRaises(ValidationError) fails ("ValidationError not raised").
"""
import uuid

from odoo.exceptions import ValidationError
from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestMcpSidecarUrlInvariant(TransactionCase):
    """#271: sidecar_backed=True ⟹ url is in-cluster (http + .svc.cluster.local)."""

    _IN_CLUSTER = 'http://inv-svc-mcp.paas-ws-inv.svc.cluster.local:3001/mcp'
    _PUBLIC = 'https://mcp.example.com/sse'
    _HTTPS_CLUSTER = 'https://inv-svc-mcp.paas-ws-inv.svc.cluster.local:3001/mcp'

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.McpServer = cls.env['woow_paas_platform.mcp_server']
        cls.Workspace = cls.env['woow_paas_platform.workspace']
        cls.Template = cls.env['woow_paas_platform.cloud_app_template']
        cls.Service = cls.env['woow_paas_platform.cloud_service']
        cls.Project = cls.env['project.project']

        cls.workspace = cls.Workspace.sudo().create({'name': 'Inv WS'})
        cls.template = cls.Template.sudo().create({
            'name': 'Inv Tmpl',
            'slug': f'inv-tpl-{uuid.uuid4().hex[:8]}',
            'helm_repo_url': 'https://charts.example.com',
            'helm_chart_name': 'inv-tpl',
            'helm_chart_version': '1.0.0',
            'min_vcpu': 1, 'min_ram_gb': 1.0, 'min_storage_gb': 1,
        })
        cls.service = cls.Service.sudo().create({
            'name': 'Inv Svc',
            'workspace_id': cls.workspace.id,
            'template_id': cls.template.id,
            'helm_release_name': 'inv-svc',
            'helm_namespace': 'paas-ws-inv',
        })
        cls.project = cls.Project.sudo().create({
            'name': 'Inv Project',
            'cloud_service_id': cls.service.id,
        })

    def _make(self, **overrides):
        vals = {
            'name': 'Inv MCP',
            'url': self._IN_CLUSTER,
            'transport': 'streamable_http',
            'scope': 'user',
            'project_id': self.project.id,
            'sidecar_backed': True,
        }
        vals.update(overrides)
        return self.McpServer.sudo().create(vals)

    # ---- helper unit ---- #
    def test_helper_is_in_cluster_url(self):
        self.assertTrue(self.McpServer._is_in_cluster_url(self._IN_CLUSTER))
        self.assertFalse(self.McpServer._is_in_cluster_url(self._PUBLIC))
        self.assertFalse(
            self.McpServer._is_in_cluster_url(self._HTTPS_CLUSTER),
            "https scheme must fail even on a cluster.local host "
            "(in-cluster sidecar is plain http)",
        )
        self.assertFalse(self.McpServer._is_in_cluster_url(''))
        self.assertFalse(self.McpServer._is_in_cluster_url('not a url'))

    # ---- constrains (sudo path — proves no su-env false-green) ---- #
    def test_sidecar_backed_public_url_rejected(self):
        with self.assertRaises(ValidationError):
            self._make(url=self._PUBLIC)

    def test_sidecar_backed_https_in_cluster_rejected(self):
        with self.assertRaises(ValidationError):
            self._make(url=self._HTTPS_CLUSTER)

    def test_sidecar_backed_in_cluster_accepted(self):
        mcp = self._make()
        self.assertTrue(mcp.exists())
        self.assertTrue(mcp.sidecar_backed)

    def test_non_sidecar_public_url_accepted(self):
        mcp = self._make(sidecar_backed=False, url=self._PUBLIC)
        self.assertTrue(mcp.exists())
        self.assertFalse(mcp.sidecar_backed)

    def test_write_flip_in_cluster_to_public_rejected(self):
        mcp = self._make()
        original_url = mcp.url
        with self.assertRaises(ValidationError):
            mcp.write({'url': self._PUBLIC})
        mcp.invalidate_recordset()
        self.assertEqual(
            mcp.url, original_url,
            "rejected write must not persist the public url",
        )
```

在 `src/tests/__init__.py` 末尾（`from . import test_p5_backfill` 之後）加：

```python
# feat: #271 internal MCP bypass guard — sidecar_backed url in-cluster invariant
from . import test_mcp_sidecar_url_invariant
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./odoo-bin -c odoo.conf -u woow_paas_platform --test-enable --test-tags "/woow_paas_platform:TestMcpSidecarUrlInvariant" --stop-after-init`

（沙盒替代：`./.claude/skills/k8s-dev-sandbox/scripts/test.sh <sandbox-name> --module woow_paas_platform`，跑整個 module。）

Expected: FAIL — `test_helper_is_in_cluster_url` 報 `AttributeError: ... has no attribute '_is_in_cluster_url'`；`test_sidecar_backed_public_url_rejected` / `test_sidecar_backed_https_in_cluster_rejected` / `test_write_flip_...` 報 `ValidationError not raised`。summary 顯示 `N failed` 或 `N error(s)`。

- [ ] **Step 3: Write minimal implementation**

在 `src/models/mcp_server.py` import 區加入 `urlparse`（Edit `old_string` → `new_string`）：

```python
# old_string
import asyncio
import json
import logging

from odoo import api, fields, models
```
```python
# new_string
import asyncio
import json
import logging
from urllib.parse import urlparse

from odoo import api, fields, models
```

在既有 `_check_scope_project_consistency` 結尾與 `def write` 之間插入 helper + 新 constrains（Edit）：

```python
# old_string
                    "project (scope='system' requires project_id=NULL)."
                    % record.name
                )

    def write(self, vals):
```
```python
# new_string
                    "project (scope='system' requires project_id=NULL)."
                    % record.name
                )

    @classmethod
    def _is_in_cluster_url(cls, url):
        """#271: True iff *url* is an in-cluster sidecar DNS endpoint —
        http scheme + hostname ending in '.svc.cluster.local'.

        Single source of truth shared by the model @api.constrains below and
        the controller-layer friendly-error check
        (paas.py:_validate_mcp_url), so the two layers never drift
        (two-layer-acl-lockstep).
        """
        try:
            parsed = urlparse(url or '')
            host = (parsed.hostname or '').strip().lower()
        except ValueError:
            return False
        return parsed.scheme == 'http' and host.endswith('.svc.cluster.local')

    @api.constrains('sidecar_backed', 'url')
    def _check_sidecar_url_in_cluster(self):
        # #271 invariant: sidecar_backed=True ⟹ url must be in-cluster DNS
        # (http + '.svc.cluster.local'). A public hostname on a sidecar-backed
        # row makes the in-cluster agent data-plane hit the CF Access login
        # page, and makes teardown's URL-prefix filter miss the row (orphan).
        # Hard invariant — NO su/group bypass (mirrors
        # _check_scope_project_consistency; su-env-exempt-guard-false-green).
        for record in self:
            if not record.sidecar_backed:
                continue
            if not self._is_in_cluster_url(record.url):
                raise ValidationError(
                    "Sidecar-backed MCP server '%s' must use an in-cluster "
                    "URL (http scheme + hostname ending in "
                    "'.svc.cluster.local'); got: %s"
                    % (record.name, record.url or '')
                )

    def write(self, vals):
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./odoo-bin -c odoo.conf -u woow_paas_platform --test-enable --test-tags "/woow_paas_platform:TestMcpSidecarUrlInvariant" --stop-after-init`

Expected: PASS — summary `0 failed, 0 error(s)`；6 個 test method 全綠。

- [ ] **Step 5: Commit**

```bash
git add src/models/mcp_server.py src/tests/test_mcp_sidecar_url_invariant.py src/tests/__init__.py
git commit -m "feat: #271 mcp_server sidecar-backed url in-cluster invariant（@api.constrains + 共用 helper）"
```

---

### Task 2: Controller 雙層 friendly error + 撤註解（設計 ②）

**Files:**
- Modify: `src/controllers/paas.py`（`_validate_mcp_url` `53-81`；call sites `1386` / `1421` / `1720` / `1758`；撤註解 `1735-1737`）
- Create: `src/tests/test_mcp_sidecar_url_invariant_endpoint.py`
- Modify: `src/tests/__init__.py`

**Interfaces:**
- Consumes: Task 1 產出的 `request.env['woow_paas_platform.mcp_server']._is_in_cluster_url(url)`。
- Produces: `_validate_mcp_url(url: str, sidecar_backed: bool = False) -> str | None`（sidecar_backed 為真且 url 非 in-cluster → 回 friendly error 字串；否則沿用既有 SSRF 判定）。

> **各端點 `sidecar_backed` 取值來源（lockstep 對齊「實際會存什麼」）**：
> - `_create_mcp_server`（legacy，`1381`）：create vals **從不寫** `sidecar_backed` → 永遠存 False → 傳 `sidecar_backed=False`（避免 false rejection）。
> - `_update_mcp_server`（legacy，`1410`）：vals loop 排除 `sidecar_backed` → flag 不可變 → 傳 **既有** `server.sidecar_backed`。此路徑無 `try/except ValidationError`，是 controller 層 friendly-error 的**唯一真 RED**（否則 model constrains raw ValidationError leak）。
> - `_create_project_mcp_server`（`1715`）：唯一從 payload 設 flag（`params.get('sidecar_backed', False)`）→ 傳同值。
> - `_update_project_mcp_server`（`1753`）：vals loop 排除 `sidecar_backed` → 傳既有 `server.sidecar_backed`。

- [ ] **Step 1: Write the failing test**

建立 `src/tests/test_mcp_sidecar_url_invariant_endpoint.py`：

```python
"""TDD — #271 controller-layer friendly error for sidecar url invariant.

Covers all 4 write endpoints (legacy + project-centric, create + update).
Design §4 ②. Reference pattern: test_mcp_external_url_creation.py.

Red expectation (after Task 1, before Task 2):
  * Legacy update path has NO try/except ValidationError, and old
    _validate_mcp_url(url) ignores the stored flag → model constrains raises
    an UNCAUGHT ValidationError → JSON-RPC body has no 'result' →
    test_legacy_update_sidecar_row_to_external_rejected fails in _post
    (AssertionError: 'result' not in body).
  * The project-centric create/update cases may already pass via Task 1's
    model @api.constrains (endpoints catch ValidationError) — that is
    defense-in-depth and expected; Task 2 adds the controller-layer error
    string + covers the legacy path.
"""
import json as json_lib
import uuid

from odoo.tests.common import HttpCase, tagged


def _new_user(env, login, name=None):
    return env['res.users'].sudo().create({
        'name': name or login,
        'login': login,
        'password': login,
        'email': f'{login}@test.example',
        'groups_id': [(6, 0, [env.ref('base.group_user').id])],
    })


_IN_CLUSTER = 'http://inv-ep-svc-mcp.paas-ws-invep.svc.cluster.local:3001/mcp'
_EXTERNAL = 'https://external.example.com/sse'


@tagged('post_install', '-at_install')
class TestMcpSidecarUrlInvariantEndpoint(HttpCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Workspace = cls.env['woow_paas_platform.workspace']
        cls.Template = cls.env['woow_paas_platform.cloud_app_template']
        cls.Service = cls.env['woow_paas_platform.cloud_service']
        cls.Project = cls.env['project.project']
        cls.McpServer = cls.env['woow_paas_platform.mcp_server']

    def setUp(self):
        super().setUp()
        # portal write flag is fail-closed off by default.
        self.env['ir.config_parameter'].sudo().set_param(
            'woow_paas_platform.portal_project_settings_editable', 'True')
        self.alice = _new_user(self.env, 'invep_alice', 'Alice (InvEp)')
        self.ws = self.Workspace.sudo().create({
            'name': 'InvEp WS', 'owner_id': self.alice.id,
        })
        self.template = self.Template.sudo().create({
            'name': 'InvEp Tmpl',
            'slug': f'invep-tpl-{uuid.uuid4().hex[:8]}',
            'helm_repo_url': 'https://charts.example.com',
            'helm_chart_name': 'invep-tpl',
            'helm_chart_version': '1.0.0',
            'min_vcpu': 1, 'min_ram_gb': 1.0, 'min_storage_gb': 1,
            'mcp_enabled': True,
            'mcp_sidecar_port': 3001,
            'mcp_transport': 'streamable_http',
            'mcp_endpoint_path': '/mcp',
        })
        self.service = self.Service.sudo().create({
            'name': 'InvEp Svc',
            'workspace_id': self.ws.id,
            'template_id': self.template.id,
            'helm_release_name': 'inv-ep-svc',
            'helm_namespace': 'paas-ws-invep',
        })
        self.project = self.Project.sudo().create({
            'name': 'InvEp Project',
            'cloud_service_id': self.service.id,
        })
        # An existing sidecar-backed in-cluster row (valid under the invariant).
        self.existing_sidecar = self.McpServer.sudo().create({
            'name': 'InvEp Existing Sidecar',
            'url': _IN_CLUSTER,
            'transport': 'streamable_http',
            'scope': 'user',
            'project_id': self.project.id,
            'sidecar_backed': True,
        })

    # ---- JSON-RPC helpers ---- #
    def _post(self, url, params):
        response = self.url_open(
            url,
            data=json_lib.dumps({
                'jsonrpc': '2.0', 'method': 'call', 'params': params,
            }),
            headers={'Content-Type': 'application/json'},
            timeout=30,
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertIn('result', body, f"Body: {body!r}")
        return body['result']

    def _project_url(self):
        return f'/api/support-projects/{self.project.id}/mcp-servers'

    def _legacy_url(self):
        return (
            f'/api/workspaces/{self.ws.id}'
            f'/services/{self.service.id}/mcp-servers'
        )

    # ---- project-centric create ---- #
    def test_project_create_sidecar_backed_external_rejected(self):
        self.authenticate('invep_alice', 'invep_alice')
        result = self._post(self._project_url(), {
            'action': 'create',
            'name': 'Backed External',
            'url': _EXTERNAL,
            'transport': 'sse',
            'sidecar_backed': True,
        })
        self.assertFalse(result.get('success'), f"Got: {result!r}")
        self.assertIn('cluster.local', (result.get('error') or '').lower())

    def test_project_create_sidecar_backed_in_cluster_ok(self):
        self.authenticate('invep_alice', 'invep_alice')
        result = self._post(self._project_url(), {
            'action': 'create',
            'name': 'Backed InCluster',
            'url': _IN_CLUSTER,
            'transport': 'streamable_http',
            'sidecar_backed': True,
        })
        self.assertTrue(result.get('success'), f"Got: {result!r}")

    # ---- project-centric update ---- #
    def test_project_update_sidecar_row_to_external_rejected(self):
        self.authenticate('invep_alice', 'invep_alice')
        result = self._post(self._project_url(), {
            'action': 'update',
            'server_id': self.existing_sidecar.id,
            'url': _EXTERNAL,
        })
        self.assertFalse(result.get('success'), f"Got: {result!r}")
        self.assertIn('cluster.local', (result.get('error') or '').lower())
        self.existing_sidecar.invalidate_recordset()
        self.assertEqual(
            self.existing_sidecar.url, _IN_CLUSTER,
            "rejected update must not persist the external url",
        )

    # ---- legacy update (the true RED — no try/except in that path) ---- #
    def test_legacy_update_sidecar_row_to_external_rejected(self):
        self.authenticate('invep_alice', 'invep_alice')
        result = self._post(self._legacy_url(), {
            'action': 'update',
            'server_id': self.existing_sidecar.id,
            'url': _EXTERNAL,
        })
        self.assertFalse(result.get('success'), f"Got: {result!r}")
        self.assertIn('cluster.local', (result.get('error') or '').lower())
        self.existing_sidecar.invalidate_recordset()
        self.assertEqual(self.existing_sidecar.url, _IN_CLUSTER)

    # ---- legacy create ignores the flag → external stays allowed ---- #
    def test_legacy_create_ignores_sidecar_flag_external_ok(self):
        self.authenticate('invep_alice', 'invep_alice')
        result = self._post(self._legacy_url(), {
            'action': 'create',
            'name': 'Legacy External',
            'url': _EXTERNAL,
            'transport': 'sse',
            'sidecar_backed': True,  # ignored by legacy create
        })
        self.assertTrue(result.get('success'), f"Got: {result!r}")
        new_id = (result.get('data') or {}).get('id')
        self.assertTrue(new_id)
        self.assertFalse(
            self.McpServer.sudo().browse(new_id).sidecar_backed,
            "legacy create must never store sidecar_backed=True (flag "
            "excluded from create vals) — so external url stays allowed",
        )
```

在 `src/tests/__init__.py` 末尾加：

```python
from . import test_mcp_sidecar_url_invariant_endpoint
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./odoo-bin -c odoo.conf -u woow_paas_platform --test-enable --test-tags "/woow_paas_platform:TestMcpSidecarUrlInvariantEndpoint" --stop-after-init`

Expected: FAIL — `test_legacy_update_sidecar_row_to_external_rejected` 失敗於 `_post` 的 `self.assertIn('result', body)`（legacy update 路徑無 `try/except`，Task 1 的 model constrains raw ValidationError leak → JSON-RPC body 只有 `error` 無 `result`）。（`test_project_create_*` / `test_project_update_*` 可能已因 Task 1 model constrains 而綠 = 雙保險，屬預期。）

- [ ] **Step 3: Write minimal implementation**

改 `_validate_mcp_url` 簽章 + 尾段加 sidecar 判定（Edit）：

```python
# old_string
def _validate_mcp_url(url: str) -> str | None:
    """Minimal SSRF guard: reject non-http(s) schemes + cloud metadata IPs."""
    if not url:
        return 'URL is required'
```
```python
# new_string
def _validate_mcp_url(url: str, sidecar_backed: bool = False) -> str | None:
    """Minimal SSRF guard: reject non-http(s) schemes + cloud metadata IPs.

    #271: when *sidecar_backed* is True, additionally require an in-cluster
    sidecar DNS URL (http + '.svc.cluster.local'); a public hostname on a
    sidecar-backed row makes the in-cluster agent data-plane hit the CF
    Access login page and makes teardown's URL-prefix filter miss the row.
    """
    if not url:
        return 'URL is required'
```

在 `_validate_mcp_url` 的最後 `return None` 之前插入 sidecar 判定（Edit）：

```python
# old_string
        if str(ip).lower() in _SSRF_BLOCKED_IPV6:
            return f'URL host not allowed: [{host}]'
    return None
```
```python
# new_string
        if str(ip).lower() in _SSRF_BLOCKED_IPV6:
            return f'URL host not allowed: [{host}]'
    if sidecar_backed and not request.env[
        'woow_paas_platform.mcp_server'
    ]._is_in_cluster_url(url):
        return (
            'Sidecar-backed MCP URL must be in-cluster (http scheme + '
            'hostname ending in .svc.cluster.local, e.g. '
            'http://<release>-mcp.<namespace>.svc.cluster.local:3001/mcp); '
            f'got: {url}'
        )
    return None
```

改 4 個 call site：

`_create_mcp_server`（`1386`）— legacy create 從不存 flag，明確傳 False（Edit）：
```python
# old_string
        if not name or not url:
            return {'success': False, 'error': 'Name and URL are required'}
        url_error = _validate_mcp_url(url)
        if url_error:
            return {'success': False, 'error': url_error}

        project = service.sudo().project_ids[:1]
```
```python
# new_string
        if not name or not url:
            return {'success': False, 'error': 'Name and URL are required'}
        # legacy create never stores sidecar_backed → validate as external.
        url_error = _validate_mcp_url(url, sidecar_backed=False)
        if url_error:
            return {'success': False, 'error': url_error}

        project = service.sudo().project_ids[:1]
```

`_update_mcp_server`（`1421`）— 用既有 stored flag（Edit）：
```python
# old_string
        if 'url' in params:
            url_error = _validate_mcp_url((params.get('url') or '').strip())
            if url_error:
                return {'success': False, 'error': url_error}

        vals = {}
        for field in ('name', 'url', 'transport', 'api_key', 'headers_json', 'description'):
```
```python
# new_string
        if 'url' in params:
            url_error = _validate_mcp_url(
                (params.get('url') or '').strip(),
                sidecar_backed=server.sidecar_backed,
            )
            if url_error:
                return {'success': False, 'error': url_error}

        vals = {}
        for field in ('name', 'url', 'transport', 'api_key', 'headers_json', 'description'):
```

`_create_project_mcp_server`（`1720`）— 用 payload flag（Edit）：
```python
# old_string
        if not name or not url:
            return {'success': False, 'error': 'Name and URL are required'}
        url_error = _validate_mcp_url(url)
        if url_error:
            return {'success': False, 'error': url_error}

        McpServer = request.env['woow_paas_platform.mcp_server'].sudo()
        try:
```
```python
# new_string
        if not name or not url:
            return {'success': False, 'error': 'Name and URL are required'}
        url_error = _validate_mcp_url(
            url, sidecar_backed=bool(params.get('sidecar_backed', False)),
        )
        if url_error:
            return {'success': False, 'error': url_error}

        McpServer = request.env['woow_paas_platform.mcp_server'].sudo()
        try:
```

`_create_project_mcp_server` 撤除 `1735-1737` 註解（Edit）：
```python
# old_string
                # Payload-driven; endpoint deliberately does not enforce
                # URL ↔ flag consistency — frontend owns the mental model.
                'sidecar_backed': bool(params.get('sidecar_backed', False)),
```
```python
# new_string
                # #271: URL↔flag consistency IS now enforced — see
                # _validate_mcp_url(sidecar_backed=...) above + the model
                # @api.constrains _check_sidecar_url_in_cluster.
                'sidecar_backed': bool(params.get('sidecar_backed', False)),
```

`_update_project_mcp_server`（`1758`）— 用既有 stored flag（Edit）：
```python
# old_string
        if 'url' in params:
            url_error = _validate_mcp_url((params.get('url') or '').strip())
            if url_error:
                return {'success': False, 'error': url_error}
        vals: dict[str, Any] = {}
```
```python
# new_string
        if 'url' in params:
            url_error = _validate_mcp_url(
                (params.get('url') or '').strip(),
                sidecar_backed=server.sidecar_backed,
            )
            if url_error:
                return {'success': False, 'error': url_error}
        vals: dict[str, Any] = {}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./odoo-bin -c odoo.conf -u woow_paas_platform --test-enable --test-tags "/woow_paas_platform:TestMcpSidecarUrlInvariantEndpoint" --stop-after-init`

Expected: PASS — summary `0 failed, 0 error(s)`；5 個 test method 全綠（含 legacy update 現在回 friendly error）。

- [ ] **Step 5: Commit**

```bash
git add src/controllers/paas.py src/tests/test_mcp_sidecar_url_invariant_endpoint.py src/tests/__init__.py
git commit -m "feat: #271 controller sidecar url in-cluster friendly error（4 端點 + 撤 URL↔flag 註解）"
```

---

### Task 3: Migration healer（設計 ④）

**Files:**
- Modify: `src/__manifest__.py`（version `18.0.1.0.42` → `18.0.1.0.43`，`:3`）
- Modify: `src/models/mcp_server.py`（加 `@api.model _heal_sidecar_backed_non_cluster_url`）
- Create: `src/migrations/18.0.1.0.43/post-migrate.py`
- Create: `src/tests/test_mcp_sidecar_url_invariant_migration.py`
- Modify: `src/tests/__init__.py`

**Interfaces:**
- Consumes: 表 `woow_paas_platform_mcp_server`（欄位 `sidecar_backed` bool、`url` char）。
- Produces: `mcp_server._heal_sidecar_backed_non_cluster_url()`（`@api.model`，row-by-row `filtered(_is_in_cluster_url)`，冪等）；`migrate(cr, version)`（`post-migrate.py`，委派上述 method）。

> **版號依據**：`src/migrations/` 現有最高 = `18.0.1.0.42`（與 manifest 同步）→ 下一版 = `18.0.1.0.43`。Odoo 只在 manifest version **高於** DB 已裝版本時才跑該版 migration，故 migration 目錄名必須 == bump 後的新 manifest version。
> **healer 判定**：用**與 constraint 同一份** `_is_in_cluster_url()`（row-by-row `filtered`），而非 raw-SQL host-only LIKE。理由（memory `two-layer-acl-lockstep`）：SQL `url NOT LIKE '%.svc.cluster.local%'` 只看 host、漏掉 `https://x.svc.cluster.local`（host 命中但 scheme≠http）——那筆 constraint 仍會擋、healer 卻跳過 → 後續 write 照炸。共用判定使「healer 收的集合 ⊇ constraint 擋的集合」，無 residual。`mcp_server` 是小的 per-tenant 表，ORM 掃描成本可忽略。

- [ ] **Step 1: Write the failing test**

建立 `src/tests/test_mcp_sidecar_url_invariant_migration.py`：

```python
"""TDD — #271 migration healer 18.0.1.0.43.

Unset sidecar_backed on rows whose url is NOT in-cluster (public regress /
legacy auto_created backfill). Preserve url + good in-cluster rows. Idempotent.
Pattern: test_mcp_project_decouple_migration_25.py.

Red expectation: src/migrations/18.0.1.0.43/post-migrate.py absent →
_load_migrate_function() returns None → _assert_migration_implemented fails.
"""
import importlib.util
import os

from odoo.tests.common import TransactionCase, tagged


_MIGRATION_PATH = os.path.abspath(
    os.path.join(
        os.path.dirname(__file__),
        '..', 'migrations', '18.0.1.0.43', 'post-migrate.py',
    )
)


def _load_migrate_function():
    if not os.path.exists(_MIGRATION_PATH):
        return None
    spec = importlib.util.spec_from_file_location(
        'mcp_sidecar_url_invariant_migration_18_0_1_0_43', _MIGRATION_PATH,
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return getattr(module, 'migrate', None)


@tagged('post_install', '-at_install')
class TestMigration43SidecarUrlHealer(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Workspace = cls.env['woow_paas_platform.workspace']
        cls.Template = cls.env['woow_paas_platform.cloud_app_template']
        cls.Service = cls.env['woow_paas_platform.cloud_service']
        cls.Project = cls.env['project.project']

    def setUp(self):
        super().setUp()
        self.migrate = _load_migrate_function()
        # sidecar_backed column must exist (impl prerequisite).
        self.env.cr.execute(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'woow_paas_platform_mcp_server' "
            "  AND column_name = 'sidecar_backed'"
        )
        if not self.env.cr.fetchone():
            self.fail("sidecar_backed column missing — Task 1 must land first.")

        self.workspace = self.Workspace.sudo().create({'name': 'WS Mig43'})
        self.template = self.Template.sudo().create({
            'name': 'Tmpl Mig43',
            'slug': 'tmpl-mig43',
            'helm_repo_url': 'https://charts.example.com',
            'helm_chart_name': 'tmpl-mig43',
            'helm_chart_version': '1.0.0',
            'min_vcpu': 1, 'min_ram_gb': 1.0, 'min_storage_gb': 1,
        })
        self.service = self.Service.sudo().create({
            'name': 'Svc Mig43',
            'workspace_id': self.workspace.id,
            'template_id': self.template.id,
            'helm_release_name': 'mig43-svc',
            'helm_namespace': 'paas-ws-mig43',
        })
        self.project = self.Project.sudo().create({
            'name': 'Bound Mig43 Project',
            'cloud_service_id': self.service.id,
        })

    def _assert_migration_implemented(self):
        self.assertIsNotNone(
            self.migrate,
            "Migration src/migrations/18.0.1.0.43/post-migrate.py is missing. "
            "Impl: delegate to mcp_server._heal_sidecar_backed_non_cluster_url().",
        )

    def _seed_row_via_sql(self, name, url, sidecar_backed):
        """Insert a user-scope row via raw SQL, bypassing the new
        @api.constrains so we can craft the pre-migration bad shape."""
        self.env.cr.execute(
            """
            INSERT INTO woow_paas_platform_mcp_server
                (name, url, transport, scope, project_id, active,
                 state, sidecar_backed,
                 create_uid, create_date, write_uid, write_date)
            VALUES
                (%s, %s, 'streamable_http', 'user', %s, TRUE,
                 'draft', %s, 1, NOW(), 1, NOW())
            RETURNING id
            """,
            (name, url, self.project.id, sidecar_backed),
        )
        return self.env.cr.fetchone()[0]

    def _read(self, row_id):
        self.env.cr.execute(
            "SELECT sidecar_backed, url FROM woow_paas_platform_mcp_server "
            "WHERE id = %s", (row_id,),
        )
        return self.env.cr.fetchone()

    def test_healer_unsets_public_sidecar_backed_row(self):
        self._assert_migration_implemented()
        bad_id = self._seed_row_via_sql(
            'Mig43 Bad', 'https://evil.example.com/mcp', True)
        good_id = self._seed_row_via_sql(
            'Mig43 Good',
            'http://mig43-svc-mcp.paas-ws-mig43.svc.cluster.local:3001/mcp',
            True)
        external_id = self._seed_row_via_sql(
            'Mig43 External', 'https://mcp.example.com/sse', False)

        self.migrate(self.env.cr, '18.0.1.0.42')

        bad_backed, bad_url = self._read(bad_id)
        self.assertFalse(
            bad_backed,
            "healer must unset sidecar_backed on the public-url row")
        self.assertEqual(
            bad_url, 'https://evil.example.com/mcp',
            "healer must preserve the url (data retained; only flag flips)")

        good_backed, _ = self._read(good_id)
        self.assertTrue(
            good_backed,
            "healer must NOT touch a valid in-cluster sidecar row")

        ext_backed, _ = self._read(external_id)
        self.assertFalse(
            ext_backed,
            "sidecar_backed=False row stays False (untouched)")

    def test_healer_unsets_https_in_cluster_row(self):
        # Residual the raw-SQL host-only LIKE would MISS but the constraint
        # rejects: host ends in .svc.cluster.local yet scheme is https (not
        # http). Row-by-row _is_in_cluster_url() catches it → healer stays in
        # lock-step with the constraint (no write-time explosion later).
        self._assert_migration_implemented()
        https_id = self._seed_row_via_sql(
            'Mig43 HttpsCluster',
            'https://mig43-svc-mcp.paas-ws-mig43.svc.cluster.local:3001/mcp',
            True)

        self.migrate(self.env.cr, '18.0.1.0.42')

        backed, url = self._read(https_id)
        self.assertFalse(
            backed,
            "healer must unset sidecar_backed on an https in-cluster row "
            "(scheme != http fails the invariant just like the constraint)")
        self.assertEqual(
            url,
            'https://mig43-svc-mcp.paas-ws-mig43.svc.cluster.local:3001/mcp',
            "url preserved")

    def test_healer_idempotent(self):
        self._assert_migration_implemented()
        bad_id = self._seed_row_via_sql(
            'Mig43 Idem', 'https://evil.example.com/mcp', True)

        self.migrate(self.env.cr, '18.0.1.0.42')
        first = self._read(bad_id)
        self.assertFalse(first[0])

        try:
            self.migrate(self.env.cr, '18.0.1.0.42')
        except Exception as exc:  # noqa: BLE001
            self.fail(f"second run raised {type(exc).__name__}: {exc}")
        self.assertEqual(first, self._read(bad_id), "state drifted on re-run")
```

在 `src/tests/__init__.py` 末尾加：

```python
from . import test_mcp_sidecar_url_invariant_migration
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./odoo-bin -c odoo.conf -u woow_paas_platform --test-enable --test-tags "/woow_paas_platform:TestMigration43SidecarUrlHealer" --stop-after-init`

Expected: FAIL — 三個 test 都在 `_assert_migration_implemented` 失敗（`Migration src/migrations/18.0.1.0.43/post-migrate.py is missing`），因 migration 檔與 model method 尚未建立。

- [ ] **Step 3: Write minimal implementation**

bump manifest version（Edit `src/__manifest__.py`）：
```python
# old_string
    "version": "18.0.1.0.42",
```
```python
# new_string
    "version": "18.0.1.0.43",
```

在 `src/models/mcp_server.py` 加 healer method（Edit；插在 `_check_sidecar_url_in_cluster` 之後、`def write` 之前）：
```python
# old_string
                    "Sidecar-backed MCP server '%s' must use an in-cluster "
                    "URL (http scheme + hostname ending in "
                    "'.svc.cluster.local'); got: %s"
                    % (record.name, record.url or '')
                )

    def write(self, vals):
```
```python
# new_string
                    "Sidecar-backed MCP server '%s' must use an in-cluster "
                    "URL (http scheme + hostname ending in "
                    "'.svc.cluster.local'); got: %s"
                    % (record.name, record.url or '')
                )

    @api.model
    def _heal_sidecar_backed_non_cluster_url(self):
        """#271 healer: unset sidecar_backed on rows whose url is NOT
        in-cluster (public regress / legacy 18.0.1.0.25 auto_created backfill
        that carried a public url). They are really external MCPs — detach
        from the CS lifecycle, keep url + tool_ids. Idempotent.

        Uses the SAME _is_in_cluster_url() as the constraint (single judgment
        source — memory two-layer-acl-lockstep). A raw-SQL host-only LIKE would
        miss 'https://x.svc.cluster.local' (host matches but scheme != http),
        which the constraint still rejects → a row the healer skips but every
        later write explodes on. Row-by-row filtered() keeps healer and
        constraint in lock-step. mcp_server is a small per-tenant table so the
        ORM scan cost is negligible.
        """
        bad = self.sudo().search([('sidecar_backed', '=', True)]).filtered(
            lambda s: not self._is_in_cluster_url(s.url or '')
        )
        if bad:
            # Writing sidecar_backed=False does NOT trip the constraint
            # (_check_sidecar_url_in_cluster skips rows where sidecar_backed is
            # falsy); it also pushes no mcp snapshot (sidecar_backed is not in
            # _MCP_SNAPSHOT_FIELDS). readonly=True blocks only the form view,
            # not ORM write.
            bad.write({'sidecar_backed': False})
            _logger.warning(
                "#271 healer: unset sidecar_backed on %s mcp_server row(s) "
                "whose url is not in-cluster (now treated as external MCP).",
                len(bad),
            )

    def write(self, vals):
```

建立 `src/migrations/18.0.1.0.43/post-migrate.py`：
```python
"""18.0.1.0.43 — #271 heal sidecar_backed rows whose url is not in-cluster.

The new @api.constrains('sidecar_backed', 'url') (sidecar-backed MCP must use
an in-cluster '.svc.cluster.local' http URL) would raise on ANY later write to
pre-existing bad rows (e.g. the 18.0.1.0.25 auto_created→sidecar_backed
backfill that may have carried a public url, or scenario E/F data). This
migration unsets sidecar_backed on those rows (they are really external MCPs —
detach from the CS lifecycle, preserve url / tool_ids). Idempotent.
"""

from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    if not version:
        return
    env = api.Environment(cr, SUPERUSER_ID, {})
    env['woow_paas_platform.mcp_server']._heal_sidecar_backed_non_cluster_url()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./odoo-bin -c odoo.conf -u woow_paas_platform --test-enable --test-tags "/woow_paas_platform:TestMigration43SidecarUrlHealer" --stop-after-init`

Expected: PASS — summary `0 failed, 0 error(s)`；`test_healer_unsets_public_sidecar_backed_row`（壞列翻 False、url 保留、in-cluster 列不動、external 列不動）、`test_healer_unsets_https_in_cluster_row`（https-in-cluster 也被收，證 healer 與 constraint 同源）與 `test_healer_idempotent`（二次跑 state 不漂移）皆綠。

- [ ] **Step 5: Commit**

```bash
git add src/__manifest__.py src/models/mcp_server.py src/migrations/18.0.1.0.43/post-migrate.py src/tests/test_mcp_sidecar_url_invariant_migration.py src/tests/__init__.py
git commit -m "feat: #271 migration healer 收既有 sidecar-backed 公開 url 壞列（18.0.1.0.43）"
```

---

### Task 4: 未部署不顯示 Add Sidecar + 後端 flag（設計 ⑥）

**Files:**
- Modify: `src/controllers/paas.py`（`_format_project_detail`，`1595-1597` 之後加 key）
- Modify: `src/static/src/paas/pages/project-detail/sections/McpServersSection.js`（`canAddSidecarMcp` getter，`44-48`）
- Create: `src/tests/test_project_detail_deployed_flag.py`
- Modify: `src/tests/__init__.py`

**Interfaces:**
- Consumes: `cloud_service.helm_release_name` / `helm_namespace`（`cloud_service.py:147-154`）——與既有兩處 sidecar URL builder（`cloud_service.py:985` `_sidecar_mcp_url`、`:1376` `_sidecar_url_prefix`）同 gate。
- Produces: project-detail payload 新 key `cloud_service_deployed: bool`；前端 `canAddSidecarMcp` 多 gate `cloud_service_deployed`。

- [ ] **Step 1: Write the failing test**

建立 `src/tests/test_project_detail_deployed_flag.py`：

```python
"""TDD — #271 project-detail payload exposes cloud_service_deployed.

Frontend gates the 'Add Sidecar MCP' button on this so an undeployed
(mcp_enabled) service can't prefill a public sidecar URL that violates the
in-cluster invariant. Design §4 ⑥ / 測試計畫 #7.

Red expectation: 'cloud_service_deployed' not yet in _format_project_detail
payload → KeyError in the assertions.
"""
import json as json_lib
import uuid

from odoo.tests.common import HttpCase, tagged


def _new_user(env, login, name=None):
    return env['res.users'].sudo().create({
        'name': name or login,
        'login': login,
        'password': login,
        'email': f'{login}@test.example',
        'groups_id': [(6, 0, [env.ref('base.group_user').id])],
    })


@tagged('post_install', '-at_install')
class TestProjectDetailDeployedFlag(HttpCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Workspace = cls.env['woow_paas_platform.workspace']
        cls.Template = cls.env['woow_paas_platform.cloud_app_template']
        cls.Service = cls.env['woow_paas_platform.cloud_service']
        cls.Project = cls.env['project.project']

    def setUp(self):
        super().setUp()
        self.env['ir.config_parameter'].sudo().set_param(
            'woow_paas_platform.portal_project_settings_editable', 'True')
        self.alice = _new_user(self.env, 'dep_alice', 'Alice (Deployed)')
        self.ws = self.Workspace.sudo().create({
            'name': 'Dep WS', 'owner_id': self.alice.id,
        })
        self.mcp_template = self.Template.sudo().create({
            'name': 'Dep Tmpl',
            'slug': f'dep-tpl-{uuid.uuid4().hex[:8]}',
            'helm_repo_url': 'https://charts.example.com',
            'helm_chart_name': 'dep-tpl',
            'helm_chart_version': '1.0.0',
            'min_vcpu': 1, 'min_ram_gb': 1.0, 'min_storage_gb': 1,
            'mcp_enabled': True,
        })
        # deployed = helm metadata present.
        self.svc_deployed = self.Service.sudo().create({
            'name': 'Dep Svc',
            'workspace_id': self.ws.id,
            'template_id': self.mcp_template.id,
            'helm_release_name': 'dep-svc',
            'helm_namespace': 'paas-ws-dep',
        })
        self.project_deployed = self.Project.sudo().create({
            'name': 'Dep Project',
            'cloud_service_id': self.svc_deployed.id,
        })
        # undeployed = no helm metadata.
        self.svc_undeployed = self.Service.sudo().create({
            'name': 'Undep Svc',
            'workspace_id': self.ws.id,
            'template_id': self.mcp_template.id,
        })
        self.project_undeployed = self.Project.sudo().create({
            'name': 'Undep Project',
            'cloud_service_id': self.svc_undeployed.id,
        })

    def _detail(self, project):
        response = self.url_open(
            f'/api/support-projects/{project.id}',
            data=json_lib.dumps({
                'jsonrpc': '2.0', 'method': 'call',
                'params': {'action': 'get'},
            }),
            headers={'Content-Type': 'application/json'},
            timeout=30,
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertIn('result', body, f"Body: {body!r}")
        return body['result']['data']

    def test_deployed_service_flag_true(self):
        self.authenticate('dep_alice', 'dep_alice')
        data = self._detail(self.project_deployed)
        self.assertTrue(data['cloud_service_deployed'])
        self.assertTrue(data['cloud_service_mcp_enabled'])

    def test_undeployed_service_flag_false(self):
        self.authenticate('dep_alice', 'dep_alice')
        data = self._detail(self.project_undeployed)
        self.assertFalse(
            data['cloud_service_deployed'],
            "undeployed (no helm metadata) service must report "
            "cloud_service_deployed=False so the frontend hides Add Sidecar")
        self.assertTrue(
            data['cloud_service_mcp_enabled'],
            "template capability flag stays True even when undeployed")
```

在 `src/tests/__init__.py` 末尾加：

```python
from . import test_project_detail_deployed_flag
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./odoo-bin -c odoo.conf -u woow_paas_platform --test-enable --test-tags "/woow_paas_platform:TestProjectDetailDeployedFlag" --stop-after-init`

Expected: FAIL — 兩個 test 都因 `data['cloud_service_deployed']` 觸發 `KeyError: 'cloud_service_deployed'`（payload 尚無此 key）。

- [ ] **Step 3: Write minimal implementation**

後端加 payload key（Edit `src/controllers/paas.py`）：
```python
# old_string
            'cloud_service_mcp_enabled': bool(
                service and service.template_id.mcp_enabled
            ),
            'workspace_id': project.workspace_id.id if project.workspace_id else False,
```
```python
# new_string
            'cloud_service_mcp_enabled': bool(
                service and service.template_id.mcp_enabled
            ),
            # #271: sidecar flow only valid once the service has an in-cluster
            # identity (helm release + namespace). Frontend gates the
            # 'Add Sidecar MCP' button on this so an undeployed service can't
            # prefill a public sidecar URL that violates the in-cluster
            # invariant (same gate as the two sidecar URL builders).
            'cloud_service_deployed': bool(
                service and service.helm_release_name and service.helm_namespace
            ),
            'workspace_id': project.workspace_id.id if project.workspace_id else False,
```

前端 getter 加第二個條件（Edit `src/static/src/paas/pages/project-detail/sections/McpServersSection.js`）：
```javascript
// old_string
    get canAddSidecarMcp() {
        // Button only visible when the backing service exposes a sidecar
        // template; endpoint still re-validates ACL on click.
        return !!this.props.project.cloud_service_mcp_enabled;
    }
```
```javascript
// new_string
    get canAddSidecarMcp() {
        // Button only visible when the backing service exposes a sidecar
        // template AND is actually deployed (has an in-cluster identity).
        // #271: an mcp_enabled-but-undeployed service would prefill a public
        // sidecar-hint URL + sidecar_backed=True → rejected by the new
        // in-cluster invariant. Gate on deployment so the sidecar flow is
        // always in-cluster. Endpoint still re-validates ACL on click.
        return (
            !!this.props.project.cloud_service_mcp_enabled &&
            !!this.props.project.cloud_service_deployed
        );
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./odoo-bin -c odoo.conf -u woow_paas_platform --test-enable --test-tags "/woow_paas_platform:TestProjectDetailDeployedFlag" --stop-after-init`

Expected: PASS — summary `0 failed, 0 error(s)`；deployed 服務 flag True、未部署服務 flag False。

- [ ] **Step 4b: 前端手動驗證（headed browser，本 repo 無 JS 測試框架）**

前端只動純 boolean getter（無新 template 語法），用 headed 瀏覽器實走驗證按鈕顯隱：

1. 部署本改動到 dev sandbox：`/woowprd:k8s-dev-sandbox up --context docker-desktop`（或既有 sandbox 重載 assets）。
2. `playwright-cli`（`open --headed`）登入 `/woow`，用 curl session 注入免打密碼（memory `woow playwright E2E gotchas`）。
3. 導航到一個 **mcp_enabled 但未部署** 的服務的 project-detail settings 頁（`/woow` 是 OWL SPA + hash 路由，深路由直接開會 404 → 須 in-app 點擊導航）→ 確認 **`Add Sidecar MCP` 按鈕不顯示**、`Add External MCP` 仍顯示。
4. 導航到一個 **已部署** 的 mcp_enabled 服務 → 確認 `Add Sidecar MCP` 按鈕顯示。
5. Console error 零容忍（root render 若壞會全站白屏）。

Expected: 未部署服務看不到 Add Sidecar，已部署看得到；console 無 error。

- [ ] **Step 5: Commit**

```bash
git add src/controllers/paas.py src/static/src/paas/pages/project-detail/sections/McpServersSection.js src/tests/test_project_detail_deployed_flag.py src/tests/__init__.py
git commit -m "feat: #271 未部署服務不顯示 Add Sidecar（cloud_service_deployed flag + 前端 gating）"
```

---

### Task 5: 刪 dead code（設計 ⑤）

**Files:**
- Modify: `src/services/paas_operator.py`（刪 `get_mcp_endpoint_url`，`846-907`，檔末最後一個 top-level def）
- Modify: `src/models/cloud_service.py`（刪 comment header + `_model_build_mcp_endpoint_url`，`971-999`）

**Interfaces:**
- Consumes: 無（純刪除）。
- Produces: 無新 public 介面。

> **0-caller 佐證（已核）**：`grep -rn "get_mcp_endpoint_url\|_model_build_mcp_endpoint_url" --include="*.py" src/` 只回 5 行 = 2 個 def + 3 個 **test docstring 提及**（`test_agent_pod_phase_a_cloud_service_mn.py:63,69`、`test_mcp_sidecar_hint_endpoint.py:216`，皆註解、非呼叫）。無 `.js`/`.xml`/string/`getattr` 間接呼叫、無 orphaned import。3 個 docstring 提及為無害註解，**保留不動**（縮小 blast radius，對齊 ground truth 建議）。本 task 無新測試，跑既有 suite 確認無 import 壞。

- [ ] **Step 1: Grep 確認 0 caller（RED-substitute：先證前提）**

Run: `grep -rn "get_mcp_endpoint_url\|_model_build_mcp_endpoint_url" --include="*.py" src/`

Expected（刪除前）：恰 5 行 —
```
src/tests/test_agent_pod_phase_a_cloud_service_mn.py:63:    """Mirror ``cloud_service._model_build_mcp_endpoint_url`` shape.
src/tests/test_agent_pod_phase_a_cloud_service_mn.py:69:    is owned by ``cloud_service._model_build_mcp_endpoint_url``).
src/tests/test_mcp_sidecar_hint_endpoint.py:216:                           (matches ``cloud_service._model_build_mcp_endpoint_url``
src/models/cloud_service.py:978:    def _model_build_mcp_endpoint_url(self):
src/services/paas_operator.py:846:def get_mcp_endpoint_url(
```
（無其他 `.py` 呼叫、無 `.js`/`.xml`；確認唯二實體是 def。）

- [ ] **Step 2: 刪 `get_mcp_endpoint_url`（`src/services/paas_operator.py`）**

先 `Read` 檔案，再 Edit 刪掉整個 function（它是檔案最後一個 top-level def，連同 def 前的兩個空行一併刪，讓檔案乾淨結束於前一個 function 的 `return PaaSOperatorClient(...)`）。刪除目標（`old_string`，含前導兩空行；`new_string` 為空字串）：

```python


def get_mcp_endpoint_url(
    env,
    sidecar_response: Optional[Dict[str, Any]] = None,
    subdomain: Optional[str] = None,
    namespace: Optional[str] = None,
    release_name: Optional[str] = None,
    endpoint_path: str = '/mcp',
) -> Optional[str]:
    """Construct the MCP endpoint URL for a cloud service's sidecar.

    .. warning::
       **DEAD CODE / DO NOT WIRE（W0 #283 覆核，2026-07-05）.** 此 helper 目前
       **無 production caller**，且優先序把公開 CF URL 排第 1/2、in-cluster 墊底。
       agent 資料面本就走 in-cluster DNS（見 decision doc
       ``agent-dataplane-in-cluster-no-cf-access-ip-bypass``），operator 回的公開
       ``mcp_endpoint_url`` 是死重（見 #301：該公開 ``{subdomain}-mcp`` route 未被
       CF Access 納管、唯靠 sidecar bearer token）。若日後真要接線取 MCP URL，
       **一律走 in-cluster 優先**，勿讓 agent 資料面 regress 成經公開 CF hostname
       撞 CF Access（#271）。

    Priority order:
    1. Use mcp_endpoint_url from sidecar patch response (operator-provided, includes Cloudflare route)
    2. Build from subdomain + PaaS domain setting (Cloudflare convention: {subdomain}-mcp.{domain})
    3. Use mcp_internal_url from sidecar patch response (K8s internal, only works in-cluster)

    For local development, users can override via port-forward:
        kubectl port-forward -n {namespace} svc/{release}-mcp {port}:{port}

    Args:
        env: Odoo environment
        sidecar_response: Response dict from PaaSOperatorClient.patch_sidecar()
        subdomain: Service subdomain (from CloudService record)
        namespace: K8s namespace (for fallback internal URL)
        release_name: Helm release name (for fallback internal URL)
        endpoint_path: MCP endpoint path (default '/mcp')

    Returns:
        MCP endpoint URL or None if cannot be determined
    """
    # 1. Try operator-provided public URL
    if sidecar_response and sidecar_response.get('mcp_endpoint_url'):
        return sidecar_response['mcp_endpoint_url']

    # 2. Build from subdomain + PaaS domain
    IrConfigParameter = env['ir.config_parameter'].sudo()
    paas_domain = IrConfigParameter.get_param('woow_paas_platform.paas_domain', '')

    if subdomain and paas_domain:
        mcp_hostname = f"{subdomain}-mcp.{paas_domain}"
        return f"https://{mcp_hostname}{endpoint_path}"

    # 3. Try internal URL from sidecar response
    if sidecar_response and sidecar_response.get('mcp_internal_url'):
        internal_url = sidecar_response['mcp_internal_url'].rstrip('/')
        return f"{internal_url}{endpoint_path}"

    _logger.warning(
        "Cannot determine MCP endpoint URL for release %s in %s. "
        "Use port-forward for local access.",
        release_name, namespace,
    )
    return None
```

> 若上述 `old_string` 因空白/縮排細節不精確匹配，改以：`Read` 檔案確認起訖行 → 用 Edit 匹配 `def get_mcp_endpoint_url(` 起至檔末 `    return None` 的實際內容刪除。刪後檔案末尾應為前一 function `get_operator_client` 的 `return PaaSOperatorClient(...)`。

- [ ] **Step 3: 刪 `_model_build_mcp_endpoint_url`（`src/models/cloud_service.py`）**

先 `Read`，再 Edit 刪 comment header（`971-976`）+ 空行 + method（`978-999`）。刪除目標（`old_string`；`new_string` 為空字串，保留前後各一空行由相鄰 section 提供的 PEP8 兩空行分隔）：

```python
    # ------------------------------------------------------------------ #
    #  MCP sidecar URL helper (model-context twin of                     #
    #  PaasController._compute_sidecar_url_hint — kept for the           #
    #  cron / non-request callers; see docs/decisions for the duplication #
    #  follow-up)                                                        #
    # ------------------------------------------------------------------ #

    def _model_build_mcp_endpoint_url(self):
        """Build MCP endpoint URL (model context, no request needed)."""
        self.ensure_one()
        template = self.template_id
        endpoint_path = template.mcp_endpoint_path or '/mcp'
        sidecar_port = template.mcp_sidecar_port or 3001

        if self.helm_release_name and self.helm_namespace:
            return (
                f"http://{self.helm_release_name}-mcp"
                f".{self.helm_namespace}.svc.cluster.local"
                f":{sidecar_port}{endpoint_path}"
            )

        if self.subdomain:
            IrConfigParameter = self.env['ir.config_parameter'].sudo()
            paas_domain = IrConfigParameter.get_param(
                'woow_paas_platform.paas_domain', 'woowtech.io',
            )
            return f"https://{self.subdomain}.{paas_domain}{endpoint_path}"

        return f"http://localhost:{sidecar_port}{endpoint_path}"
```

> 刪後確認前一 method 末與下一段 section comment 之間為 PEP8 兩空行分隔（Read 目測 969-1001 區）。

- [ ] **Step 4: 驗證無壞（byte-compile + re-grep + 針對性 suite）**

Run:
```bash
python -m py_compile src/services/paas_operator.py src/models/cloud_service.py
grep -rn "get_mcp_endpoint_url\|_model_build_mcp_endpoint_url" --include="*.py" src/
```

Expected:
- `py_compile` 無輸出（成功）。
- `grep` 只剩 3 行 test docstring 提及（`test_agent_pod_phase_a_cloud_service_mn.py:63,69`、`test_mcp_sidecar_hint_endpoint.py:216`）；**0 個 def、0 個呼叫**。

> 註：完整模組測試留到 Task 6 之後跑（此時 Task 6 尚未做 → orthogonality 既有測試會 stale-red，屬預期）。本 task 以 `py_compile` + `grep` 證明無 import 壞、無殘留 caller 即可；被刪 method 只被 test docstring 提及（非呼叫），既有測試不受影響。

- [ ] **Step 5: Commit**

```bash
git add src/services/paas_operator.py src/models/cloud_service.py
git commit -m "refactor: #271 刪除 public-first MCP endpoint dead code（get_mcp_endpoint_url + _model_build_mcp_endpoint_url）"
```

---

### Task 6: Teardown 對稱測試 + 反轉既有 orthogonality 斷言（設計 ③ + 測試補）

**Files:**
- Modify: `src/tests/test_mcp_sidecar_backed_lifecycle.py`（加 teardown 對稱 guard 測試至 class `TestSidecarBackedLifecycleFilter`）
- Modify: `src/tests/test_mcp_sidecar_hint_endpoint.py`（反轉 `480-536` 兩個 orthogonality 測試）

**Interfaces:**
- Consumes: 既有 `_seed_service_with_dual_user_mcps`（`test_mcp_sidecar_backed_lifecycle.py:90-135`）、`cloud_service._collect_sidecar_user_mcps` / `_sidecar_url_prefix`（`cloud_service.py:1350-1381`）；既有 hint-endpoint fixture（Alice / `project_bound_mcp` / `_post` / `_url_mcp_servers`）。
- Produces: 無新介面；反轉 `test_create_endpoint_does_not_enforce_url_consistency_backed_true_external` 決策（#271 明確 enforce），補 teardown 前綴 match guard。

> **背景**：Task 1 model constrains 一落地，`test_mcp_sidecar_hint_endpoint.py` 的 `test_create_endpoint_does_not_enforce_url_consistency_backed_true_external`（現斷言 sidecar_backed=True+external **成功**）即轉 red（project create 端點 catch ValidationError → 回 success=False）。本 task 正式反轉該斷言，避免留假綠繞過新 gate（memory `credential-claim-test-fixture-coupling` / `phase-versioned-test-assertion`）。相鄰兩個 flag-persist 測試（`398-446` True-persist 用 in-cluster url、`448-478` False-persist 用 external url + sidecar_backed=False）在新 invariant 下**維持綠**，不動。

- [ ] **Step 1: 確認既有 orthogonality 測試已 red（Task 1 之後）**

Run: `./odoo-bin -c odoo.conf -u woow_paas_platform --test-enable --test-tags "/woow_paas_platform:TestSidecarHintEndpoint" --stop-after-init`

Expected: FAIL — `test_create_endpoint_does_not_enforce_url_consistency_backed_true_external` 失敗（`assertTrue(result.get('success'))` 但端點現在因 model constrains 回 success=False）。其餘同 class 測試綠。

- [ ] **Step 2: 反轉 orthogonality 斷言 + 加 teardown 對稱 guard**

反轉第一個測試（Edit `src/tests/test_mcp_sidecar_hint_endpoint.py`，method 名 + docstring + 斷言全換）：
```python
# old_string
    def test_create_endpoint_does_not_enforce_url_consistency_backed_true_external(self):
        """AC #28: endpoint does NOT enforce
        ``sidecar_backed=True ⇔ url is cluster.local``.

        Per plan §AC #28: 「Endpoint **不** enforce 一致性 ... frontend
        負責 mental model 一致」. This test exercises the deliberately
        loose combination ``sidecar_backed=True + URL points at external
        host``; create must still succeed.

        **Red today**：same as the True-flag test — payload key is
        currently ignored.
        """
        self.authenticate('shp_alice', 'shp_alice')
        result = self._post(
            self._url_mcp_servers(self.project_bound_mcp.id),
            {
                'action': 'create',
                'name': 'Inconsistent External-but-Backed MCP',
                'url': 'https://external.example.com/sse',
                'transport': 'sse',
                'sidecar_backed': True,
            },
        )
        self.assertTrue(
            result.get('success'),
            "AC #28: sidecar_backed=True + external URL must NOT be "
            "rejected by the endpoint. Frontend owns the mental model. "
            f"Got: {result!r}.",
        )
```
```python
# new_string
    def test_create_endpoint_enforces_sidecar_url_in_cluster_backed_true_external(self):
        """#271 (reverses AC #28): endpoint now ENFORCES
        ``sidecar_backed=True ⟹ url is in-cluster``.

        Pre-#271 this combination (sidecar_backed=True + external URL) was
        deliberately allowed ("frontend owns the mental model"). #271 reverses
        that: a sidecar-backed row pointing at a public hostname makes the
        in-cluster agent data-plane hit the CF Access login page and makes
        teardown's URL-prefix filter miss the row (orphan). Create must now be
        rejected — controller friendly error + model @api.constrains.
        """
        self.authenticate('shp_alice', 'shp_alice')
        result = self._post(
            self._url_mcp_servers(self.project_bound_mcp.id),
            {
                'action': 'create',
                'name': 'Inconsistent External-but-Backed MCP',
                'url': 'https://external.example.com/sse',
                'transport': 'sse',
                'sidecar_backed': True,
            },
        )
        self.assertFalse(
            result.get('success'),
            "#271: sidecar_backed=True + external URL must be REJECTED. "
            f"Got: {result!r}.",
        )
        self.assertIn(
            'cluster.local', (result.get('error') or '').lower(),
            "#271: rejection error must explain the in-cluster requirement. "
            f"Got: {result!r}.",
        )
```

更新第二個測試 method 名 + docstring（Edit；斷言 body 維持 `assertTrue`，只換首兩行）：
```python
# old_string
    def test_create_endpoint_does_not_enforce_url_consistency_backed_false_cluster_local(self):
        """AC #28: reverse direction —
        ``sidecar_backed=False`` + URL is cluster.local must also succeed.

        Some users may copy an existing sidecar URL into the External
        form by hand; endpoint must accept it without forcing the flag
        to True. Tests `(sidecar_backed, url-shape)` pair is orthogonal.
        """
```
```python
# new_string
    def test_create_endpoint_allows_backed_false_cluster_local(self):
        """#271: the invariant is one-directional
        (``sidecar_backed=True ⟹ in-cluster``). The reverse is NOT enforced:
        ``sidecar_backed=False`` + a cluster.local URL must still succeed
        (a user may hand-copy an existing sidecar URL into the External form;
        we don't auto-upgrade the flag or reject the URL). Orthogonality is
        preserved on the False side only.
        """
```

加 teardown 對稱 guard 測試（Edit `src/tests/test_mcp_sidecar_backed_lifecycle.py`；`Read` 後把此 method 插入 class `TestSidecarBackedLifecycleFilter`，例如接在 `test_unlink_override_preserves_external_user_mcp` 之後）：
```python
    def test_sidecar_prefix_match_collects_in_cluster_row(self):
        """#271 teardown symmetry: the in-cluster invariant guarantees a
        sidecar_backed=True row's URL starts with the service's
        _sidecar_url_prefix(), so _collect_sidecar_user_mcps() finds it via
        prefix match (no orphan). External (sidecar_backed=False) rows are
        NOT collected. Guards §3 of the design (teardown is a natural
        consequence of the invariant).
        """
        service, project, sidecar_mcp, external_mcp = (
            self._seed_service_with_dual_user_mcps(
                name_prefix='PrefixMatch', release_name='prefix-match-svc',
            )
        )
        prefix = service._sidecar_url_prefix()
        self.assertIsNotNone(
            prefix,
            "deployed service (helm metadata set) must expose a non-None "
            "sidecar URL prefix",
        )
        collected = service._collect_sidecar_user_mcps(prefix)
        self.assertIn(
            sidecar_mcp, collected,
            "#271: sidecar_backed in-cluster row must be collected by the "
            "prefix filter (invariant guarantees url startswith prefix).",
        )
        self.assertNotIn(
            external_mcp, collected,
            "external (sidecar_backed=False) row must NOT be collected.",
        )
```

- [ ] **Step 3: Run test to verify it passes**

Run: `./odoo-bin -c odoo.conf -u woow_paas_platform --test-enable --test-tags "/woow_paas_platform:TestSidecarHintEndpoint,/woow_paas_platform:TestSidecarBackedLifecycleFilter" --stop-after-init`

Expected: PASS — summary `0 failed, 0 error(s)`；反轉後的 `test_create_endpoint_enforces_sidecar_url_in_cluster_backed_true_external`、維持綠的 `test_create_endpoint_allows_backed_false_cluster_local`、新增的 `test_sidecar_prefix_match_collects_in_cluster_row` 皆綠。

- [ ] **Step 4: 全模組回歸（此時整套應一致綠）**

Run: `./odoo-bin -c odoo.conf -u woow_paas_platform --test-enable --test-tags "/woow_paas_platform" --stop-after-init`

（沙盒替代：`./.claude/skills/k8s-dev-sandbox/scripts/test.sh <sandbox-name> --module woow_paas_platform`。）

Expected: PASS — 全模組 `0 failed, 0 error(s)`（Task 1-6 一致，無 stale-red）。

- [ ] **Step 5: Commit**

```bash
git add src/tests/test_mcp_sidecar_hint_endpoint.py src/tests/test_mcp_sidecar_backed_lifecycle.py
git commit -m "test: #271 teardown 對稱 + 反轉 url↔flag 正交既有斷言"
```

---

## Appendix：設計 ①-⑥ / 測試 8 項 → Task 對應表

| Design §4 | 內容 | Task |
| --- | --- | --- |
| ① | Model 層主守衛 `@api.constrains` + 共用 `_is_in_cluster_url` helper | Task 1 |
| ② | Controller friendly error（4 端點）+ 撤 `1735-1737` 註解 | Task 2 |
| ③ | Teardown 對稱性（invariant 的自然結果，僅加測試） | Task 6 |
| ④ | Migration healer（既有壞列 unset，`18.0.1.0.43`） | Task 3 |
| ⑤ | Dead code 清理（2 段 0-caller） | Task 5 |
| ⑥ | 收斂 sidecar-hint public fallback（前端 gating + 後端 flag） | Task 4 |

| Design §6 測試 | 內容 | Task |
| --- | --- | --- |
| 1 | model constrains（4 組合：public reject / in-cluster pass / https-cluster reject / external pass） | Task 1 |
| 2 | sudo 也擋（su-env 非假綠） | Task 1（`_make` 全走 `.sudo().create`，直接驗 su path） |
| 3 | controller 4 端點 friendly error | Task 2 |
| 4 | regress 案例（既有 in-cluster 列 update url→public 擋） | Task 2（project + legacy update）＋ Task 1（`test_write_flip_...`） |
| 5 | teardown 對稱 | Task 6 |
| 6 | migration healer（壞列翻 False、資料保留、冪等） | Task 3 |
| 7 | 情境 E 回歸（未部署 flag=False → Add Sidecar 不可用） | Task 4（後端 flag 測試 + 前端 headed 驗證） |
| 8 | 更新既有 orthogonality 測試（反轉決策，勿留假綠） | Task 6 |
