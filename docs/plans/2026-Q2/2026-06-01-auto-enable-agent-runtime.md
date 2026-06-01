---
title: Auto-enable Agent Runtime on Project Creation
category: plans
status: draft
date: 2026-06-01
branch: feat/project-setting
---

# Auto-enable Agent Runtime on Project Creation — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 讓使用者在 `/woow` 開 project 時，平台若已就緒就自動把 `use_agent_runtime` 設成 True，免去進 Odoo 後台手動開關；agent_template 由已合併的「平台預設 template」（PR #112）接手，使用者零設定即可用 agent pod。

**Architecture:** 三層改動 —(1) `res.config.settings` 新增平台層 Boolean 總開關 `woow_default_enable_agent_runtime`（config_parameter，admin 設一次）；(2) `_create_project` 在建立時做 fail-safe 就緒檢查（總開關 ON + operator url/api_key 已設 + 平台預設 template 合格），通過才把 `use_agent_runtime=True` 寫進 create vals；(3) 一次性 migration 把既有 project 也 flip（就緒平台）。`use_agent_runtime` 欄位 model default 維持 False（保住 Phase A 休眠 invariant 與既有 schema/regression test 全綠），啟用只發生在 controller / migration 的 server 端決策——官方建立路徑不從使用者輸入讀此旗標（model 層可寫性與 self-scoped 安全分析見下方「已知範圍邊界 / 安全邊界」）。

**Tech Stack:** Odoo 18 addon（Python 4-space、OWL 不涉及）；Odoo `res.config.settings` + `ir.config_parameter` pattern；Odoo migration runner（`src/migrations/<version>/post-migrate.py`）；`odoo.tests.common.TransactionCase` + `@tagged('post_install','-at_install')`。

---

## 背景與成因（rebase develop 後）

- `use_agent_runtime` 是 `project.project` 上的 Boolean，`default=False`（`src/models/project_project.py:69`），agent-runtime-pod epic Phase A 故意休眠；唯一暴露入口是 Odoo 後台 project 表單（`src/views/project_views.xml:8-17`），`/woow` 沒有。
- 建立 project 的後端 `_create_project`（`src/controllers/ai_assistant.py:1258`）vals 只塞 `name/description/cloud_service_id`，完全不帶 agent 欄位 → 新 project 一律 False。
- **template 那關已被 PR #112 解掉**：admin 在 Settings 設一個「Default Agent Template」，`_resolve_agent_template`（`ai_assistant.py:755`，④ fallback at 774-804）讓所有沒自綁的 project 自動套用平台預設（含 `is_public + published + 無 project_ids` 跨租戶防護）。
- 因此本 branch 只剩唯一卡點：**`use_agent_runtime` 沒有非後台的啟用路徑**。

## 設計決策（使用者 2026-06-01 拍板）

| # | 決策 | 選擇 |
|---|------|------|
| 1 | 啟用機制 | **建立時自動開 + 平台層總開關控制**（新增 `woow_default_enable_agent_runtime`） |
| 2 | 就緒把關 | **fail-safe**：總開關 ON **且** operator url+api_key 已設 **且** 平台預設 template 合格，才自動開；否則維持 False（不讓使用者一聊天就撞 `no_template`/`spawn_failed`） |
| 3 | 既有 project | **加一次性 migration** flip 既有（就緒平台），並順手把總開關設 ON 讓新建路徑一致 |
| 4 | 欄位 default | **不動**（維持 `default=False`）——啟用只在 controller/migration server 端，保住 Phase A schema test（SSE.1 / A.8）與安全面 |

## 就緒（readiness）定義

「就緒」沿用 chat-time gating 鏈中 Odoo 可控的條件，確保自動開的 project 聊天有機會成功：

1. 平台總開關 `woow_paas_platform.default_enable_agent_runtime` == True（**僅建立路徑要求；migration 不要求，見下**）。
2. operator 已設定：`get_agent_runtime_client(env) is not None`（讀 `operator_url` + `operator_api_key`，`agent_runtime_client.py:168-175`）。
3. 平台預設 template 合格：`_platform_default_agent_template(env)` 非空（`is_public + published + 無 project_ids + exists`，與 `_resolve_agent_template ④` 同一驗證）。

> **不在就緒檢查內、但 runtime 仍需要的部署層**（`AGENT_RUNTIME_NAMESPACE_SECRET` / `AGENT_RUNTIME_AUTH_SECRET_CURRENT` env、NetworkPolicy）屬 deployment，已由 PR #110 `create.sh` stg mode 持久化，見 `docs/tracking/pitfalls/agent-runtime-stg-enablement-gap-chain.md`。本 plan 不碰；但 operator url/api_key 已設是「admin 已走過平台 setup」的強訊號，足以當建立路徑的就緒代理。

## 已知範圍邊界

- migration 是**一次性**（升級到 18.0.1.0.30 時跑一次）。**升級當下平台尚未就緒**（operator/預設 template 還沒設）的環境，既有 project 不會被回填；之後 admin 設好平台 + 開總開關 → **新** project 自動開，既有的需手動（後台）或日後另開 backfill。stg/prod 等「已在用 agent runtime」的環境升級時即就緒，migration 會正確回填——符合決策 3 的意圖。
- `use_agent_runtime` 不進任何使用者可寫的 controller update 白名單（決策 1 是「建立自動 + 平台總開關」，非「`/woow` per-project 自助 toggle」）；官方建立／更新路徑不從使用者輸入讀此旗標 → **官方 UI 路徑**無提權面。

### 安全邊界（review 揭露，務必記住）

`use_agent_runtime`（與鄰近的 `external_mcp_allowed`）在 **model 層對 workspace member 物理可寫**：`src/security/ir.model.access.csv:27` 給 `base.group_user` 對 `project.project` RWCU=1，`src/security/ir_rules.xml:75` `project_rule_workspace_member` `perm_write=True`。因此 member 可直接 ORM/RPC `write({'use_agent_runtime': True})` **繞過** controller 就緒閘——controller 白名單只守官方 UI 路徑，守不住直接 ORM/RPC。

**刻意不加 `project.project.write()` guard**：flip 只影響翻動者**自己** project 的 chat 路由，解析的仍是該 project 自己綁定或 guarded 的平台預設 template，最壞只是平台未就緒時讓自己 project 進入 `no_template`/`spawn_failed`（self-DoS），**不跨租戶外洩、不提權**。

這個「不加 guard」是**有條件**的決策，前提是這兩個欄位語意維持「per-project 行為開關」。**失效條件**：若 `use_agent_runtime`/`external_mcp_allowed` 日後語意演進為會影響**跨租戶可見範圍**（例：可解析非自己綁定的 template、或 egress 放寬後可外連他人端點），**必須**回頭在 `project.project.write()` 補 guard。見 `docs/decisions/patterns/scope-flip-write-guard-not-ir-rule.md`——本 case 是該 pattern「不適用第二項：flip 不造成跨租戶外洩/提權」的實例，而非反例。

---

## File Structure

| 檔案 | 責任 | 動作 |
|------|------|------|
| `src/models/res_config_settings.py` | 平台總開關欄位 | Modify：+1 Boolean field |
| `src/views/res_config_settings_views.xml` | 總開關 UI row | Modify：+1 checkbox row（AI Assistant Configuration block 內） |
| `src/hooks.py` | 卸載清理 config_parameter | Modify：`_CONFIG_PARAMETERS` +1 行 |
| `src/controllers/ai_assistant.py` | 就緒判斷 + 建立時自動開 + 既有回填 helper + `_resolve_agent_template` DRY 重構 | Modify |
| `src/migrations/18.0.1.0.30/post-migrate.py` | 一次性回填既有 project | Create |
| `src/__manifest__.py` | 版號 bump（觸發 migration） | Modify：`18.0.1.0.29` → `18.0.1.0.30` |
| `src/tests/test_auto_enable_agent_runtime.py` | 就緒 helper + 建立路徑 + 回填 helper 測試 | Create |
| `src/tests/__init__.py` | 註冊新 test 模組 | Modify：+1 import |

共用 helper（module-level，放在 `ai_assistant.py`，可被 controller 與 migration import）：

- `_platform_default_agent_template(env) -> recordset`：合格平台預設 template 或空 recordset。`_resolve_agent_template ④` 重構成呼叫它（DRY）。
- `_agent_runtime_autoenable_ready(env) -> bool`：建立路徑就緒（含總開關）。
- `backfill_existing_projects_use_agent_runtime(env) -> int`：migration 用（不含總開關；就緒時把總開關設 ON + flip 既有，回傳 flip 數）。

---

## Task 1：平台總開關欄位（res.config.settings）

**Files:**
- Modify: `src/models/res_config_settings.py`（接在 `woow_default_agent_template_id` 之後，約 line 57）
- Test: `src/tests/test_auto_enable_agent_runtime.py`

- [ ] **Step 1: 先寫失敗測試（欄位 round-trip 到 config_parameter）**

建立 `src/tests/test_auto_enable_agent_runtime.py`，先放 import 與這個測試：

```python
"""Auto-enable agent runtime on project creation.

決策來源：docs/plans/2026-Q2/2026-06-01-auto-enable-agent-runtime.md
Covers:
  T1   res.config.settings.woow_default_enable_agent_runtime round-trips through
       ir.config_parameter 'woow_paas_platform.default_enable_agent_runtime'.
  R-*  _agent_runtime_autoenable_ready fail-safe gate.
  C-*  _create_project auto-enables only when ready.
  B-*  backfill_existing_projects_use_agent_runtime one-time migration helper.
"""
from unittest.mock import patch

from odoo.tests.common import TransactionCase, tagged

_TOGGLE = 'woow_paas_platform.default_enable_agent_runtime'
_URL = 'woow_paas_platform.operator_url'
_KEY = 'woow_paas_platform.operator_api_key'
_DEFTMPL = 'woow_paas_platform.default_agent_template_id'


@tagged('post_install', '-at_install')
class TestAutoEnableToggleSetting(TransactionCase):
    """T1: the Settings Boolean persists to ir.config_parameter."""

    def test_t1_toggle_round_trips_to_config_parameter(self):
        settings = self.env['res.config.settings'].sudo().create({
            'woow_default_enable_agent_runtime': True,
        })
        settings.execute()
        stored = self.env['ir.config_parameter'].sudo().get_param(_TOGGLE)
        self.assertEqual(
            stored, 'True',
            "Settings field must persist True to the '%s' config_parameter."
            % _TOGGLE,
        )
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `./odoo-bin -c odoo.conf --test-enable --test-tags woow_paas_platform.TestAutoEnableToggleSetting --stop-after-init -u woow_paas_platform`
（本機請用 k8s-dev-sandbox `test.sh`；見「測試執行」段）
Expected: FAIL — `Invalid field 'woow_default_enable_agent_runtime'`。

- [ ] **Step 3: 加欄位**

在 `src/models/res_config_settings.py`，緊接 `woow_default_agent_template_id`（約 line 57，`)` 結尾）之後插入：

```python
    woow_default_enable_agent_runtime = fields.Boolean(
        string='Auto-enable Agent Runtime for New Projects',
        config_parameter='woow_paas_platform.default_enable_agent_runtime',
        default=False,
        help='When ON, a project created from /woow automatically turns on '
             'its Agent Runtime Pod — but only if the platform is ready '
             '(PaaS Operator URL + API Key set and a valid Default Agent '
             'Template selected above). When the platform is not ready the '
             'flag stays off so a new project never lands in a guaranteed '
             'spawn-failure state. Users never set this per project; it is a '
             'platform-wide default decided here.',
    )
```

- [ ] **Step 4: 跑測試確認通過**

Run: 同 Step 2。
Expected: PASS。

- [ ] **Step 5: Commit**

```bash
git add src/models/res_config_settings.py src/tests/test_auto_enable_agent_runtime.py
git commit -m "feat: add platform toggle to auto-enable agent runtime for new projects"
```

---

## Task 2：總開關 UI row + 卸載清理

**Files:**
- Modify: `src/views/res_config_settings_views.xml`（AI Assistant Configuration block，default_agent_template_id row 之後、Test Connection button 之前）
- Modify: `src/hooks.py`（`_CONFIG_PARAMETERS`，約 line 123-129）
- Modify: `src/tests/test_auto_enable_agent_runtime.py`

- [ ] **Step 1: 寫失敗測試（卸載清理清單含新 key）**

在 test 檔追加：

```python
@tagged('post_install', '-at_install')
class TestAutoEnableUninstallCleanup(TransactionCase):
    """The new config_parameter must be in hooks._CONFIG_PARAMETERS so an
    addon uninstall clears it (CLAUDE.md: config_parameter is not auto-cleaned).
    """

    def test_toggle_param_in_uninstall_cleanup_list(self):
        from odoo.addons.woow_paas_platform import hooks
        self.assertIn(
            _TOGGLE, hooks._CONFIG_PARAMETERS,
            "default_enable_agent_runtime must be in _CONFIG_PARAMETERS so "
            "uninstall removes it.",
        )
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `... --test-tags woow_paas_platform.TestAutoEnableUninstallCleanup ...`
Expected: FAIL — assertIn，key 不在清單。

- [ ] **Step 3: 加 config_parameter 到 hooks**

在 `src/hooks.py` `_CONFIG_PARAMETERS` list，緊接 `'woow_paas_platform.default_agent_template_id',`（line 129）之後插入：

```python
    'woow_paas_platform.default_enable_agent_runtime',
```

- [ ] **Step 4: 跑測試確認通過**

Run: 同 Step 2。Expected: PASS。

- [ ] **Step 5: 加 UI row（view）**

在 `src/views/res_config_settings_views.xml`，把 `woow_default_agent_template_id` 的 `<div class="row mt8">`（line 113-117）之後、`<div class="mt8">`（Test Connection button，line 118）之前插入：

```xml
                                <div class="row mt8">
                                    <label for="woow_default_enable_agent_runtime" class="col-lg-3 o_light_label"/>
                                    <field name="woow_default_enable_agent_runtime"
                                           class="col-lg-6"/>
                                </div>
```

> View 無法用 unit test 斷言內容意義，靠 module upgrade 不報 parse error 驗證。Step 6 的 upgrade 會載入此 view。

- [ ] **Step 6: Commit**

```bash
git add src/views/res_config_settings_views.xml src/hooks.py src/tests/test_auto_enable_agent_runtime.py
git commit -m "feat: surface auto-enable toggle in settings + register config_parameter cleanup"
```

---

## Task 3：就緒 helper + 平台預設 template helper（DRY 重構）

**Files:**
- Modify: `src/controllers/ai_assistant.py`（module-level helpers；`_resolve_agent_template` ④ block at 774-804 重構）
- Modify: `src/tests/test_auto_enable_agent_runtime.py`

- [ ] **Step 1: 寫失敗測試（就緒 helper 的 fail-safe 矩陣）**

在 test 檔追加：

```python
@tagged('post_install', '-at_install')
class TestAutoEnableReadiness(TransactionCase):
    """R-*: _agent_runtime_autoenable_ready — fail-safe gate matrix."""

    def setUp(self):
        super().setUp()
        from odoo.addons.woow_paas_platform.controllers import ai_assistant
        self.mod = ai_assistant
        self.ICP = self.env['ir.config_parameter'].sudo()
        self.AgentTemplate = self.env['woow_paas_platform.agent_template'].sudo()

    def _make_valid_default(self):
        tmpl = self.AgentTemplate.create({
            'name': 'Platform Default', 'is_public': True, 'state': 'published',
        })
        self.ICP.set_param(_DEFTMPL, str(tmpl.id))
        return tmpl

    def _set_ready(self):
        """All three readiness conditions satisfied."""
        self.ICP.set_param(_TOGGLE, 'True')
        self.ICP.set_param(_URL, 'http://paas-operator:8000')
        self.ICP.set_param(_KEY, 'secret-key')
        self._make_valid_default()

    def test_r1_all_conditions_met_is_ready(self):
        self._set_ready()
        self.assertTrue(self.mod._agent_runtime_autoenable_ready(self.env))

    def test_r2_toggle_off_not_ready(self):
        self._set_ready()
        self.ICP.set_param(_TOGGLE, 'False')
        self.assertFalse(self.mod._agent_runtime_autoenable_ready(self.env))

    def test_r3_operator_url_missing_not_ready(self):
        self._set_ready()
        self.ICP.set_param(_URL, '')
        self.assertFalse(self.mod._agent_runtime_autoenable_ready(self.env))

    def test_r4_operator_key_missing_not_ready(self):
        self._set_ready()
        self.ICP.set_param(_KEY, '')
        self.assertFalse(self.mod._agent_runtime_autoenable_ready(self.env))

    def test_r5_no_default_template_not_ready(self):
        self._set_ready()
        self.ICP.set_param(_DEFTMPL, '')
        self.assertFalse(self.mod._agent_runtime_autoenable_ready(self.env))

    def test_r6_invalid_default_template_not_ready(self):
        # draft template = not a valid platform default
        self.ICP.set_param(_TOGGLE, 'True')
        self.ICP.set_param(_URL, 'http://paas-operator:8000')
        self.ICP.set_param(_KEY, 'secret-key')
        draft = self.AgentTemplate.create({
            'name': 'Draft', 'is_public': True, 'state': 'draft',
        })
        self.ICP.set_param(_DEFTMPL, str(draft.id))
        self.assertFalse(self.mod._agent_runtime_autoenable_ready(self.env))
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `... --test-tags woow_paas_platform.TestAutoEnableReadiness ...`
Expected: FAIL — `module ... has no attribute '_agent_runtime_autoenable_ready'`。

- [ ] **Step 3: 加 module-level helpers + 重構 `_resolve_agent_template`**

在 `src/controllers/ai_assistant.py` **module 層**（class 外，建議放在 `class AiAssistantController` 定義之前的 module helpers 區，或檔案尾端 module level）新增：

```python
def _platform_default_agent_template(env):
    """Return the platform default agent_template if valid, else empty.

    Valid = config_parameter set + record exists + is_public + published +
    project-unbound. Mirrors the _resolve_agent_template ④ guard exactly so a
    project-bound template can never leak that tenant's MCP/skill bindings
    cluster-wide when served as the global default.
    """
    AgentTemplate = env['woow_paas_platform.agent_template'].sudo()
    default_id = env['ir.config_parameter'].sudo().get_param(
        'woow_paas_platform.default_agent_template_id'
    )
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


def _agent_runtime_autoenable_ready(env):
    """True when a project created now should auto-enable use_agent_runtime.

    Fail-safe gate (plan 決策 2): platform toggle ON **and** operator
    configured (url+api_key) **and** a valid platform default template — so
    auto-enable never lands a project in a guaranteed no_template /
    spawn_failed state. Users never write use_agent_runtime; this is the only
    place that flips it on the /woow create path.
    """
    from ..services.agent_runtime_client import get_agent_runtime_client
    ICP = env['ir.config_parameter'].sudo()
    toggle = (ICP.get_param(
        'woow_paas_platform.default_enable_agent_runtime') or '').strip().lower()
    if toggle not in ('true', '1'):
        return False
    if get_agent_runtime_client(env) is None:
        return False
    if not _platform_default_agent_template(env):
        return False
    return True
```

然後把 `_resolve_agent_template` 的 ④ block（`ai_assistant.py:774-804`，從 `if not template:` 到 `return template or None` 之前的整段）替換成 DRY 版本：

```python
        if not template:
            # ④ platform default from Settings (config_parameter). Last
            # fallback so a project's own binding wins. The validity guard
            # (public + published + project-unbound, cross-tenant leak guard)
            # lives in _platform_default_agent_template so the create-path
            # readiness check and this resolution agree by construction.
            template = _platform_default_agent_template(request.env)
        return template or None
```

> 注意：`_resolve_agent_template` 開頭已 `AgentTemplate = request.env[...].sudo()`，重構後 ④ 改用 helper（helper 內自取 sudo），不影響 ①②③。

- [ ] **Step 4: 跑測試確認通過（新 + 既有 resolution 回歸）**

Run（新）：`... --test-tags woow_paas_platform.TestAutoEnableReadiness ...` → PASS。
Run（回歸）：`... --test-tags woow_paas_platform.TestDefaultAgentTemplateResolution ...` → 仍全 PASS（④ 行為等價）。
Expected: 兩者皆 PASS。

- [ ] **Step 5: Commit**

```bash
git add src/controllers/ai_assistant.py src/tests/test_auto_enable_agent_runtime.py
git commit -m "feat: add agent-runtime auto-enable readiness gate + DRY platform-default resolution"
```

---

## Task 4：建立 project 時自動開（`_create_project`）

**Files:**
- Modify: `src/controllers/ai_assistant.py`（`_create_project`，vals 建好後、`create(vals)` 前，約 line 1267→1280）
- Modify: `src/tests/test_auto_enable_agent_runtime.py`

- [ ] **Step 1: 寫失敗測試（建立路徑：就緒才開）**

在 test 檔追加（沿用既有 test_default_agent_template 的 `patch.object(module, 'request', _Req)` 技巧，讓 `request.env` 指向 test env；`_create_project` 內 `request.env.user.partner_id` 在 test 會是當前 admin user，`message_subscribe` 正常）：

```python
@tagged('post_install', '-at_install')
class TestAutoEnableOnCreate(TransactionCase):
    """C-*: _create_project flips use_agent_runtime only when ready."""

    def setUp(self):
        super().setUp()
        from odoo.addons.woow_paas_platform.controllers import ai_assistant
        self.mod = ai_assistant
        self.controller = ai_assistant.AiAssistantController()
        self.ICP = self.env['ir.config_parameter'].sudo()
        self.AgentTemplate = self.env['woow_paas_platform.agent_template'].sudo()

    def _set_ready(self):
        self.ICP.set_param(_TOGGLE, 'True')
        self.ICP.set_param(_URL, 'http://paas-operator:8000')
        self.ICP.set_param(_KEY, 'secret-key')
        tmpl = self.AgentTemplate.create({
            'name': 'Platform Default', 'is_public': True, 'state': 'published',
        })
        self.ICP.set_param(_DEFTMPL, str(tmpl.id))

    def _create(self, name):
        test_env = self.env

        class _Req:
            env = test_env

        with patch.object(self.mod, 'request', _Req()):
            return self.controller._create_project({'name': name})

    def test_c1_ready_platform_auto_enables(self):
        self._set_ready()
        res = self._create('c1-ready')
        self.assertTrue(res['success'])
        project = self.env['project.project'].browse(res['data']['id'])
        self.assertTrue(
            project.use_agent_runtime,
            "On a ready platform a newly created project must auto-enable "
            "use_agent_runtime.",
        )

    def test_c2_toggle_off_does_not_enable(self):
        self._set_ready()
        self.ICP.set_param(_TOGGLE, 'False')
        res = self._create('c2-toggleoff')
        project = self.env['project.project'].browse(res['data']['id'])
        self.assertFalse(
            project.use_agent_runtime,
            "Toggle off → no auto-enable even if operator + default template "
            "are configured.",
        )

    def test_c3_not_ready_does_not_enable(self):
        # toggle on but operator unconfigured → fail-safe stays off
        self.ICP.set_param(_TOGGLE, 'True')
        res = self._create('c3-notready')
        project = self.env['project.project'].browse(res['data']['id'])
        self.assertFalse(
            project.use_agent_runtime,
            "Unready platform must NOT auto-enable (would guarantee "
            "spawn_failed/no_template).",
        )
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `... --test-tags woow_paas_platform.TestAutoEnableOnCreate ...`
Expected: FAIL — `test_c1` 紅（project.use_agent_runtime 仍 False，因為 `_create_project` 還沒寫旗標）。

- [ ] **Step 3: 在 `_create_project` 加自動開**

在 `src/controllers/ai_assistant.py` `_create_project`，把 vals 補完（cloud_service / workspace 分支之後）、`project = request.env['project.project'].sudo().create(vals)`（line 1280）**之前**插入：

```python
        # Server-decided auto-enable (plan 決策 1+2). use_agent_runtime is NEVER
        # read from user params — only flipped here when the platform is ready,
        # so there is no attacker-writable path to widen agent-runtime scope.
        if _agent_runtime_autoenable_ready(request.env):
            vals['use_agent_runtime'] = True

```

- [ ] **Step 4: 跑測試確認通過**

Run: `... --test-tags woow_paas_platform.TestAutoEnableOnCreate ...`
Expected: PASS（c1 開、c2/c3 維持 False）。

- [ ] **Step 5: Commit**

```bash
git add src/controllers/ai_assistant.py src/tests/test_auto_enable_agent_runtime.py
git commit -m "feat: auto-enable use_agent_runtime on project create when platform ready"
```

---

## Task 5：既有 project 回填 helper + migration

**Files:**
- Modify: `src/controllers/ai_assistant.py`（module-level `backfill_existing_projects_use_agent_runtime`）
- Create: `src/migrations/18.0.1.0.30/post-migrate.py`
- Modify: `src/__manifest__.py`（版號 bump）
- Modify: `src/tests/test_auto_enable_agent_runtime.py`

- [ ] **Step 1: 寫失敗測試（回填 helper）**

在 test 檔追加：

```python
@tagged('post_install', '-at_install')
class TestBackfillExistingProjects(TransactionCase):
    """B-*: backfill_existing_projects_use_agent_runtime (migration helper)."""

    def setUp(self):
        super().setUp()
        from odoo.addons.woow_paas_platform.controllers import ai_assistant
        self.mod = ai_assistant
        self.ICP = self.env['ir.config_parameter'].sudo()
        self.AgentTemplate = self.env['woow_paas_platform.agent_template'].sudo()

    def _make_ready_platform(self):
        # NOTE: backfill is toggle-INDEPENDENT (toggle is off at upgrade time);
        # readiness = operator configured + valid default template.
        self.ICP.set_param(_URL, 'http://paas-operator:8000')
        self.ICP.set_param(_KEY, 'secret-key')
        tmpl = self.AgentTemplate.create({
            'name': 'Platform Default', 'is_public': True, 'state': 'published',
        })
        self.ICP.set_param(_DEFTMPL, str(tmpl.id))

    def test_b1_ready_flips_existing_and_sets_toggle(self):
        self._make_ready_platform()
        proj = self.env['project.project'].sudo().create(
            {'name': 'b1-existing'})
        self.assertFalse(proj.use_agent_runtime)
        flipped = self.mod.backfill_existing_projects_use_agent_runtime(self.env)
        self.assertGreaterEqual(flipped, 1)
        self.assertTrue(
            proj.use_agent_runtime,
            "Backfill on a ready platform must flip existing projects.",
        )
        self.assertEqual(
            self.ICP.get_param(_TOGGLE), 'True',
            "Backfill must also turn the new-project default toggle ON so the "
            "create path stays consistent post-migration.",
        )

    def test_b2_not_ready_is_noop(self):
        # operator unconfigured → backfill must do nothing
        proj = self.env['project.project'].sudo().create(
            {'name': 'b2-existing'})
        flipped = self.mod.backfill_existing_projects_use_agent_runtime(self.env)
        self.assertEqual(flipped, 0)
        self.assertFalse(proj.use_agent_runtime)
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `... --test-tags woow_paas_platform.TestBackfillExistingProjects ...`
Expected: FAIL — `module ... has no attribute 'backfill_existing_projects_use_agent_runtime'`。

- [ ] **Step 3: 加回填 helper**

在 `src/controllers/ai_assistant.py` module 層（緊接 `_agent_runtime_autoenable_ready` 之後）新增：

```python
def backfill_existing_projects_use_agent_runtime(env):
    """One-time backfill for migration 18.0.1.0.30.

    On a platform that already has agent runtime fully configured (operator
    url+api_key + a valid platform default template), turn on the new-project
    default toggle and flip every existing project's use_agent_runtime to True.

    Toggle-INDEPENDENT on purpose: the toggle is brand-new (defaults off) at
    upgrade time, so gating on it would no-op. A platform with operator + a
    valid default template configured is already running agent runtime → it is
    safe (and intended, plan 決策 3) to backfill. Returns the number of
    projects flipped (0 when the platform is not ready).
    """
    from ..services.agent_runtime_client import get_agent_runtime_client
    if get_agent_runtime_client(env) is None:
        return 0
    if not _platform_default_agent_template(env):
        return 0
    env['ir.config_parameter'].sudo().set_param(
        'woow_paas_platform.default_enable_agent_runtime', 'True')
    projects = env['project.project'].sudo().search([
        ('use_agent_runtime', '!=', True)])
    if projects:
        projects.write({'use_agent_runtime': True})
    return len(projects)
```

- [ ] **Step 4: 跑測試確認通過**

Run: `... --test-tags woow_paas_platform.TestBackfillExistingProjects ...`
Expected: PASS。

- [ ] **Step 5: 建立 migration**

Create `src/migrations/18.0.1.0.30/post-migrate.py`：

```python
"""18.0.1.0.30 — one-time backfill: auto-enable agent runtime on ready platforms.

決策來源：docs/plans/2026-Q2/2026-06-01-auto-enable-agent-runtime.md (決策 3).

If this environment already has agent runtime fully configured (PaaS Operator
url+api_key + a valid platform Default Agent Template), enable the new-project
default toggle and flip every existing project's use_agent_runtime to True so
existing projects match the new auto-enable behaviour. No-op (and safe) on
environments that are not yet configured — they only get the new-project
default once an admin finishes platform setup and turns the toggle on.
"""
import logging

from odoo import SUPERUSER_ID, api
from odoo.addons.woow_paas_platform.controllers.ai_assistant import (
    backfill_existing_projects_use_agent_runtime,
)

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    flipped = backfill_existing_projects_use_agent_runtime(env)
    _logger.info(
        "18.0.1.0.30 auto-enable backfill: flipped %s existing project(s) "
        "(0 = platform not yet configured, no-op).", flipped,
    )
```

- [ ] **Step 6: Bump 版號（觸發 migration）**

在 `src/__manifest__.py`：

```python
    "version": "18.0.1.0.30",
```
（原為 `"18.0.1.0.29"`，line 3。）

- [ ] **Step 7: 升級驗證 migration（就緒 sandbox）**

Run（k8s-dev-sandbox，平台已設好 operator + default template 的環境）：
`./.claude/skills/k8s-dev-sandbox/scripts/test.sh <sandbox> --module woow_paas_platform`
並升級：模組 upgrade 後 `kubectl ... logs` 應見 `18.0.1.0.30 auto-enable backfill: flipped N existing project(s)`。
Expected: migration 無 error；就緒環境 N>=1 且 toggle 變 True；未就緒環境 N=0 不報錯。

- [ ] **Step 8: Commit**

```bash
git add src/controllers/ai_assistant.py src/migrations/18.0.1.0.30/post-migrate.py src/__manifest__.py src/tests/test_auto_enable_agent_runtime.py
git commit -m "feat: backfill existing projects' use_agent_runtime on ready platforms (migration 18.0.1.0.30)"
```

---

## Task 6：註冊新 test 模組 + 全模組回歸

**Files:**
- Modify: `src/tests/__init__.py`

- [ ] **Step 1: 註冊新 test 模組**

在 `src/tests/__init__.py` 加（依現有 import 風格，與 `test_default_agent_template` 並列）：

```python
from . import test_auto_enable_agent_runtime
```

- [ ] **Step 2: 跑本 feature 全部測試**

Run: `... --test-tags woow_paas_platform.TestAutoEnableToggleSetting,woow_paas_platform.TestAutoEnableUninstallCleanup,woow_paas_platform.TestAutoEnableReadiness,woow_paas_platform.TestAutoEnableOnCreate,woow_paas_platform.TestBackfillExistingProjects ...`
Expected: 全 PASS。

- [ ] **Step 3: 跑 agent-runtime 相關回歸（守 Phase A/C/D invariant 不破）**

Run: `... --test-tags woow_paas_platform.TestPhaseCSseFlagAndFrontendContract,woow_paas_platform.TestDefaultAgentTemplateResolution,woow_paas_platform.TestDefaultAgentTemplateSettings ...`
另跑 Phase A schema（`test_agent_pod_phase_a_project_schema` A.8）與 Phase D（`test_agent_pod_phase_d_enablement` UI.1）。
Expected: 全 PASS —— `use_agent_runtime` 欄位 default 仍 False（SSE.1 / A.8），form view 仍含 toggle（UI.1）。

- [ ] **Step 4: 全模組測試**

Run: `./.claude/skills/k8s-dev-sandbox/scripts/test.sh <sandbox> --module woow_paas_platform`
Expected: 無新增 FAIL。

- [ ] **Step 5: Commit**

```bash
git add src/tests/__init__.py
git commit -m "test: register auto-enable agent runtime test module"
```

---

## 測試執行

- **首選**：k8s-dev-sandbox（CLAUDE.md 規定本地 e2e/整合測試走 sandbox，不用 docker compose）：
  - `./.claude/skills/k8s-dev-sandbox/scripts/create.sh --context <ctx>`
  - `./.claude/skills/k8s-dev-sandbox/scripts/test.sh <sandbox> --module woow_paas_platform`
- 上面各 Task 的 `--test-tags ...` 指令是邏輯示意（Odoo 用 class name 當 tag 需確認；亦可整模組 test）。實跑以 sandbox `test.sh` 為準。

## Self-Review（對照 spec）

- **決策 1（建立自動 + 平台總開關）**：Task 1（欄位）+ Task 4（建立時讀就緒寫旗標）✓
- **決策 2（fail-safe 就緒）**：Task 3（`_agent_runtime_autoenable_ready` 三條件 + R-* 矩陣）✓
- **決策 3（migration 回填既有 + 設 toggle）**：Task 5（helper B-* + migration + 版號）✓
- **決策 4（欄位 default 不動）**：全程未改 `project_project.py:69`；Task 6 Step 3 明確回歸 SSE.1 / A.8 ✓
- **跨租戶防護不退化**：`_platform_default_agent_template` 保留 `is_public + published + 無 project_ids` 三條件，`_resolve_agent_template ④` DRY 改呼叫它，行為等價，`TestDefaultAgentTemplateResolution` 回歸守住 ✓
- **安全面**：`use_agent_runtime` 不進使用者可寫白名單，只 server 端決策；Task 4 Step 3 註解明示 ✓
- **DRY**：template 合格驗證單一來源（resolution / 就緒 / 回填共用）✓
- **Placeholder 掃描**：各 step 皆含完整 code 與預期輸出，無 TBD/TODO ✓
