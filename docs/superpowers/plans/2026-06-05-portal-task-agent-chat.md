# Portal Task Agent Chat 實作計畫

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在原生 Odoo portal task/project 頁（read-mode share）加 AI chat 區塊與 Agent status 卡片，最大化重用既有 `/woow` chat 管線。

**Architecture:** 後端在既有 access helpers 加「portal 正向判定分支」（model 層 predicate 鏡射原生 portal ir.rule，two-layer-acl-lockstep），portal controller 繼承 `ProjectCustomerPortal` 注入 view values 並 lazy 建 channel + join；前端用 Odoo 18 `public_components` registry 在 QWeb 頁掛 OWL 元件，直接重用 `ai_service.js` / `markdown_parser.js` / marked+purify lib（同檔案進 `web.assets_frontend`），SSE 協議消費抽成共用 `ai_stream.js`。

**Tech Stack:** Odoo 18（QWeb inherit、HttpCase、ir.rule mirror）、OWL（public_components）、SSE EventSource、marked + DOMPurify。

**Spec:** `docs/superpowers/specs/2026-06-05-portal-task-agent-chat-design.md`（已核可）

---

## 執行前提

- **工作目錄隔離**：執行時用 `superpowers:using-git-worktrees` skill 建 worktree（或既有 maestri Floor）。本 plan 的 base branch 是 `feat/protal-agent-project`。**不要**在 repo 主目錄直接做事。
- **測試執行方式**（二擇一）：
  - K8s sandbox（推薦）：`./.claude/skills/k8s-dev-sandbox/scripts/create.sh --context docker-desktop` 建沙盒後 `./.claude/skills/k8s-dev-sandbox/scripts/test.sh <sandbox-name> --module woow_paas_platform`
  - 本機 odoo-server：`cd /Users/eugene/Documents/woow/AREA-odoo/odoo-server && ./odoo-bin -c odoo.conf --test-enable --test-tags /woow_paas_platform:<TestClass> --stop-after-init`
- **背景知識**（執行卡住時讀）：
  - Chat 架構：`docs/guides/development/agent-runtime-chat-architecture.md`（特別是 §0 two-hop bridge、§3.1 並發分層）
  - ACL 紀律：`docs/decisions/patterns/two-layer-acl-lockstep.md`、`docs/decisions/patterns/orm-vs-endpoint-acl-test-coverage.md`
  - 原生 portal ir.rule：`odoo-18.0/addons/project/security/project_security.xml:187-246`
- **plan-gap 紀律**：遇到 plan 沒涵蓋的決策點或測試假設與真實 API 不符 → **停下回報**，不要硬塞（`docs/decisions/patterns/plan-gap-stop-and-report.md`）。

## 檔案結構總覽

| 檔案 | 動作 | 職責 |
|---|---|---|
| `src/models/project_task.py` | 修改 | `_portal_partner_can_access`（task portal predicate）+ `_agent_task_context_for_channel` |
| `src/models/project_project.py` | 修改 | `_portal_partner_can_access`（project portal predicate + task-leg 延伸） |
| `src/controllers/ai_assistant.py` | 修改 | 3 個 access helper 加 portal 分支；task context 接線（legacy + runtime payload） |
| `src/controllers/portal.py` | 新增 | `PaasProjectCustomerPortal`：view values 注入 + channel ensure/join |
| `src/controllers/__init__.py` | 修改 | import portal |
| `src/views/portal_templates.xml` | 新增 | QWeb xpath 繼承（task 頁 status+chat、project 頁 status） |
| `src/__manifest__.py` | 修改 | data 加 portal_templates.xml；assets 加 `web.assets_frontend` |
| `src/static/src/paas/services/ai_stream.js` | 新增 | 共用 SSE 協議 consumer |
| `src/static/src/paas/components/ai-chat/AiChat.js` | 修改 | `startStream()` 改用 `ai_stream.js`（協議邏輯單一來源） |
| `src/static/src/portal/portal_agent_status.{js,xml,scss}` | 新增 | Agent status 卡片（public component） |
| `src/static/src/portal/portal_task_chat.{js,xml,scss}` | 新增 | Portal chat 元件（public component） |
| `src/tests/test_portal_agent_access.py` | 新增 | model predicate 真值表（TransactionCase） |
| `src/tests/test_portal_agent_endpoints.py` | 新增 | endpoint cross-user（HttpCase） |
| `src/tests/test_portal_agent_page.py` | 新增 | portal 頁渲染 + auto-join（HttpCase） |
| `src/tests/test_agent_task_context.py` | 新增 | task context hook（TransactionCase） |
| `docs/tracking/followups/follow-ups.md` | 修改 | F-PORTAL-1（invoker_role 'guest' 語意）追蹤 |

---

### Task 1: Model 層 portal access predicates（TDD）

**Files:**
- Modify: `src/models/project_task.py`（146 行，文末加 method）
- Modify: `src/models/project_project.py`（文末加 method）
- Test: `src/tests/test_portal_agent_access.py`（新增）

**背景**：原生 portal ir.rule（`project_security.xml`）：task rule = `project_id.privacy_visibility='portal' AND active AND (message_partner_ids child_of user.commercial OR project collaborator(non-limited) child_of user.commercial)`；project rule = `privacy_visibility='portal' AND message_partner_ids child_of user.commercial`。predicate 放 model 層（非 controller）讓 TransactionCase 可直測（repo 慣例，同 #111 snapshot 下沉）。

- [ ] **Step 1: 寫失敗測試**

建立 `src/tests/test_portal_agent_access.py`：

```python
"""Portal-leg access predicate truth tables (two-layer-acl-lockstep Layer 2).

Mirrors the native portal ir.rules so the controller carve-out can never
silently diverge from the rule layer:

* project.task     ``_portal_partner_can_access``  ⇄ project.project_task_rule_portal
* project.project  ``_portal_partner_can_access``  ⇄ project.project_project_rule_portal
                                                     (+ deliberate task-leg extension)
"""
import uuid

from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestPortalAgentAccess(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        suffix = uuid.uuid4().hex[:8]
        Partner = cls.env['res.partner']
        User = cls.env['res.users']
        portal_group = cls.env.ref('base.group_portal')

        cls.partner_alice = Partner.create({
            'name': f'Alice Portal {suffix}',
            'email': f'alice-{suffix}@test.example',
        })
        cls.partner_bob = Partner.create({
            'name': f'Bob Portal {suffix}',
            'email': f'bob-{suffix}@test.example',
        })
        cls.portal_alice = User.create({
            'login': f'portal_alice_{suffix}',
            'name': 'Alice',
            'partner_id': cls.partner_alice.id,
            'groups_id': [(6, 0, [portal_group.id])],
        })

        cls.project = cls.env['project.project'].create({
            'name': f'Portal Proj {suffix}',
            'privacy_visibility': 'portal',
        })
        cls.task = cls.env['project.task'].create({
            'name': f'Portal Task {suffix}',
            'project_id': cls.project.id,
        })

    # ---- task predicate -------------------------------------------------- #

    def test_task_follower_allowed(self):
        self.task.message_subscribe(partner_ids=[self.partner_alice.id])
        self.assertTrue(
            self.task._portal_partner_can_access(self.partner_alice))

    def test_task_non_follower_denied(self):
        self.assertFalse(
            self.task._portal_partner_can_access(self.partner_bob))

    def test_task_denied_when_visibility_not_portal(self):
        self.task.message_subscribe(partner_ids=[self.partner_alice.id])
        self.project.privacy_visibility = 'followers'
        self.assertFalse(
            self.task._portal_partner_can_access(self.partner_alice))

    def test_task_collaborator_allowed_limited_denied(self):
        collab = self.env['project.collaborator'].create({
            'project_id': self.project.id,
            'partner_id': self.partner_alice.id,
        })
        self.assertTrue(
            self.task._portal_partner_can_access(self.partner_alice))
        collab.limited_access = True
        self.assertFalse(
            self.task._portal_partner_can_access(self.partner_alice))

    def test_task_archived_denied(self):
        self.task.message_subscribe(partner_ids=[self.partner_alice.id])
        self.task.active = False
        self.assertFalse(
            self.task._portal_partner_can_access(self.partner_alice))

    def test_task_without_project_denied(self):
        orphan = self.env['project.task'].create({'name': 'orphan'})
        orphan.message_subscribe(partner_ids=[self.partner_alice.id])
        self.assertFalse(
            orphan._portal_partner_can_access(self.partner_alice))

    # ---- project predicate ----------------------------------------------- #

    def test_project_follower_allowed(self):
        self.project.message_subscribe(partner_ids=[self.partner_alice.id])
        self.assertTrue(
            self.project._portal_partner_can_access(self.partner_alice))

    def test_project_task_leg_extension(self):
        """Task-only share grants project-level agent status/chat access."""
        self.task.message_subscribe(partner_ids=[self.partner_alice.id])
        self.assertTrue(
            self.project._portal_partner_can_access(self.partner_alice))

    def test_project_stranger_denied(self):
        self.assertFalse(
            self.project._portal_partner_can_access(self.partner_bob))

    def test_project_denied_when_visibility_not_portal(self):
        self.project.message_subscribe(partner_ids=[self.partner_alice.id])
        self.project.privacy_visibility = 'employees'
        self.assertFalse(
            self.project._portal_partner_can_access(self.partner_alice))
```

- [ ] **Step 2: 跑測試確認紅**

Run: `--test-tags /woow_paas_platform:TestPortalAgentAccess`
Expected: FAIL — `AttributeError: 'project.task' object has no attribute '_portal_partner_can_access'`

- [ ] **Step 3: 實作 predicates**

`src/models/project_task.py` 文末（class 內）加：

```python
    def _portal_partner_can_access(self, partner):
        """Portal-leg access predicate — Python mirror of the native ir.rule
        ``project.project_task_rule_portal`` (two-layer-acl-lockstep Layer 2).

        Layer 1 (ir.rule, native, UNTOUCHED by this addon):
            project_id.privacy_visibility = 'portal' AND active
            AND (message_partner_ids child_of user.commercial_partner
                 OR project collaborator(limited_access=False) child_of it)

        Any change here MUST stay in lock-step with that rule (and vice
        versa) — silent divergence re-opens the IDOR surface. Positive-only:
        portal users must never fall through to the internal fail-open legs
        in the controller helpers (F4).
        """
        self.ensure_one()
        task = self.sudo()
        project = task.project_id
        if not project or project.privacy_visibility != 'portal' or not task.active:
            return False
        commercial = partner.commercial_partner_id
        allowed = self.env['res.partner'].sudo().search(
            [('id', 'child_of', commercial.id)])
        if task.message_partner_ids & allowed:
            return True
        return bool(project.collaborator_ids.filtered(
            lambda c: c.partner_id in allowed and not c.limited_access))
```

`src/models/project_project.py` 的 `ProjectProject` class 內加（檔案找 `_inherit = 'project.project'` 的 class）：

```python
    def _portal_partner_can_access(self, partner):
        """Portal-leg project predicate (two-layer-acl-lockstep Layer 2).

        Mirror of ``project.project_project_rule_portal`` PLUS a deliberate
        task-leg extension: a partner shared on ANY active task of this
        project may read the project's agent status / chat with the project
        agent on that task. Required because chat & agent status are
        project-scoped while native share can be task-scoped. This widens
        ONLY the controller-computed agent endpoints — the project record
        itself stays invisible (native ir.rule untouched). See design spec
        §安全模型.
        """
        self.ensure_one()
        project = self.sudo()
        if project.privacy_visibility != 'portal':
            return False
        commercial = partner.commercial_partner_id
        allowed = self.env['res.partner'].sudo().search(
            [('id', 'child_of', commercial.id)])
        if project.message_partner_ids & allowed:
            return True
        if project.collaborator_ids.filtered(
                lambda c: c.partner_id in allowed and not c.limited_access):
            return True
        return bool(self.env['project.task'].sudo().search_count([
            ('project_id', '=', project.id),
            ('active', '=', True),
            ('message_partner_ids', 'in', allowed.ids),
        ]))
```

- [ ] **Step 4: 跑測試確認綠**

Run: `--test-tags /woow_paas_platform:TestPortalAgentAccess`
Expected: 10 tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/models/project_task.py src/models/project_project.py src/tests/test_portal_agent_access.py
git commit -m "feat: add portal-leg access predicates mirroring native portal ir.rules"
```

---

### Task 2: Controller access helpers 的 portal 分支（TDD, HttpCase）

**Files:**
- Modify: `src/controllers/ai_assistant.py:81-124`（3 個 helper）
- Test: `src/tests/test_portal_agent_endpoints.py`（新增）

**背景**：`_check_channel_access`（:81）目前只看 channel membership；`_check_project_access`（:99）有兩個 internal fail-open（無 workspace → True、`_check_task_access` 無 project → True）。portal 分支必須**先行**且**正向**。stale-membership：portal user 被取消分享後仍是 channel member → 必須 deny（每請求重驗 task predicate）。

- [ ] **Step 1: 寫失敗測試**

建立 `src/tests/test_portal_agent_endpoints.py`：

```python
"""Endpoint-level portal access tests (HttpCase — \"ORM 綠 ≠ endpoint secure\").

Cross-user negatives for every portal-reachable AI endpoint:
chat/history, chat/post, agent-status, agent-pod-status.
(SSE /api/ai/stream is covered in test_portal_agent_page.py Task 3.)
"""
import json
import uuid

from odoo.tests.common import HttpCase, tagged

PASSWORD = 'Portal!234'


@tagged('post_install', '-at_install')
class TestPortalAgentEndpoints(HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        suffix = uuid.uuid4().hex[:8]
        cls.suffix = suffix
        Partner = cls.env['res.partner']
        User = cls.env['res.users']
        portal_group = cls.env.ref('base.group_portal')

        cls.partner_alice = Partner.create({
            'name': f'EP Alice {suffix}', 'email': f'ep-a-{suffix}@t.example'})
        cls.partner_bob = Partner.create({
            'name': f'EP Bob {suffix}', 'email': f'ep-b-{suffix}@t.example'})
        cls.alice = User.create({
            'login': f'ep_alice_{suffix}', 'name': 'Alice', 'password': PASSWORD,
            'partner_id': cls.partner_alice.id,
            'groups_id': [(6, 0, [portal_group.id])]})
        cls.bob = User.create({
            'login': f'ep_bob_{suffix}', 'name': 'Bob', 'password': PASSWORD,
            'partner_id': cls.partner_bob.id,
            'groups_id': [(6, 0, [portal_group.id])]})

        cls.project = cls.env['project.project'].create({
            'name': f'EP Proj {suffix}', 'privacy_visibility': 'portal'})
        cls.task = cls.env['project.task'].create({
            'name': f'EP Task {suffix}', 'project_id': cls.project.id,
            'ai_auto_reply': False})
        # Alice is shared on the task (read-mode share semantics).
        cls.task.message_subscribe(partner_ids=[cls.partner_alice.id])
        # Channel exists and BOTH portal partners are members — proves that
        # membership alone must NOT be sufficient for portal users.
        cls.channel = cls.task._create_chat_channel()
        cls.channel.sudo().write({'channel_member_ids': [
            (0, 0, {'partner_id': cls.partner_alice.id}),
            (0, 0, {'partner_id': cls.partner_bob.id}),
        ]})

    def _rpc(self, url, params):
        resp = self.url_open(
            url,
            data=json.dumps({'jsonrpc': '2.0', 'method': 'call',
                             'params': params, 'id': 1}),
            headers={'Content-Type': 'application/json'},
            timeout=20,
        )
        self.assertEqual(resp.status_code, 200)
        return resp.json().get('result') or {}

    # ---- chat history / post --------------------------------------------- #

    def test_history_shared_portal_ok(self):
        self.authenticate(self.alice.login, PASSWORD)
        res = self._rpc('/api/ai/chat/history',
                        {'channel_id': self.channel.id})
        self.assertTrue(res.get('success'), res)

    def test_history_member_but_unshared_denied(self):
        """Stale channel membership without task share must be denied."""
        self.authenticate(self.bob.login, PASSWORD)
        res = self._rpc('/api/ai/chat/history',
                        {'channel_id': self.channel.id})
        self.assertFalse(res.get('success'))
        self.assertEqual(res.get('error'), 'Access denied')

    def test_post_shared_portal_ok(self):
        self.authenticate(self.alice.login, PASSWORD)
        res = self._rpc('/api/ai/chat/post',
                        {'channel_id': self.channel.id, 'body': 'hello'})
        self.assertTrue(res.get('success'), res)

    def test_post_member_but_unshared_denied(self):
        self.authenticate(self.bob.login, PASSWORD)
        res = self._rpc('/api/ai/chat/post',
                        {'channel_id': self.channel.id, 'body': 'hi'})
        self.assertFalse(res.get('success'))
        self.assertEqual(res.get('error'), 'Access denied')

    def test_post_revoked_share_denied(self):
        """Un-share revokes access on the NEXT request despite membership."""
        self.authenticate(self.alice.login, PASSWORD)
        self.task.message_unsubscribe(partner_ids=[self.partner_alice.id])
        res = self._rpc('/api/ai/chat/post',
                        {'channel_id': self.channel.id, 'body': 'hi'})
        self.assertFalse(res.get('success'))
        # restore for other tests (class-level fixture)
        self.task.message_subscribe(partner_ids=[self.partner_alice.id])

    # ---- agent status ------------------------------------------------------ #

    def test_agent_status_shared_portal_ok(self):
        self.authenticate(self.alice.login, PASSWORD)
        res = self._rpc('/api/ai/agent-status',
                        {'project_id': self.project.id})
        self.assertTrue(res.get('success'), res)
        self.assertEqual(res['data']['scope'], 'project')

    def test_agent_status_unshared_portal_denied(self):
        self.authenticate(self.bob.login, PASSWORD)
        res = self._rpc('/api/ai/agent-status',
                        {'project_id': self.project.id})
        self.assertFalse(res.get('success'))
        self.assertEqual(res.get('error'), 'Access denied')

    def test_agent_pod_status_shared_portal_ok(self):
        self.authenticate(self.alice.login, PASSWORD)
        res = self._rpc('/api/ai/agent-pod-status',
                        {'project_id': self.project.id})
        self.assertTrue(res.get('success'), res)

    def test_agent_pod_status_unshared_portal_denied(self):
        self.authenticate(self.bob.login, PASSWORD)
        res = self._rpc('/api/ai/agent-pod-status',
                        {'project_id': self.project.id})
        self.assertFalse(res.get('success'))
        self.assertEqual(res.get('error'), 'Access denied')

    # ---- internal regression ----------------------------------------------- #

    def test_internal_non_task_channel_unchanged(self):
        """Internal users on non-task channels keep membership-only access."""
        admin = self.env.ref('base.user_admin')
        ch = self.env['discuss.channel'].sudo().create({
            'name': f'free-{self.suffix}', 'channel_type': 'channel',
            'channel_partner_ids': [(4, admin.partner_id.id)]})
        self.authenticate('admin', 'admin')
        res = self._rpc('/api/ai/chat/history', {'channel_id': ch.id})
        self.assertTrue(res.get('success'), res)

    def test_portal_non_task_channel_denied(self):
        """Portal users may only use task-bound channels."""
        ch = self.env['discuss.channel'].sudo().create({
            'name': f'free2-{self.suffix}', 'channel_type': 'channel',
            'channel_partner_ids': [(4, self.partner_alice.id)]})
        self.authenticate(self.alice.login, PASSWORD)
        res = self._rpc('/api/ai/chat/history', {'channel_id': ch.id})
        self.assertFalse(res.get('success'))
```

- [ ] **Step 2: 跑測試確認紅**

Run: `--test-tags /woow_paas_platform:TestPortalAgentEndpoints`
Expected: `test_history_member_but_unshared_denied`、`test_post_member_but_unshared_denied`、`test_post_revoked_share_denied`、`test_agent_status_unshared_portal_denied`、`test_agent_pod_status_unshared_portal_denied`、`test_portal_non_task_channel_denied` **FAIL**（目前 membership 即放行 / `_check_project_access` 對 portal 誤判）；其餘正向與 regression case PASS。

> 注意：`test_agent_*_unshared_portal_denied` 在現行程式可能「碰巧紅」也可能「碰巧綠」（取決於 fail-open 路徑）— 跑完記下實際紅綠分佈，與上面預期不符就回報，不要直接改測試。

- [ ] **Step 3: 實作 portal 分支**

`src/controllers/ai_assistant.py` — 替換 `_check_channel_access`（:81-87）：

```python
    def _check_channel_access(self, channel):
        """Verify the current user has access to the channel.

        Internal users: channel membership (unchanged).
        Portal users (``user.share``): membership AND the task-level portal
        predicate re-validated on EVERY request — stale channel membership
        after un-share must not keep the door open (design spec §安全模型 7).
        Portal users may only use task-bound channels.
        """
        user = request.env.user
        user_partner = user.partner_id
        if user_partner not in channel.channel_partner_ids:
            return False
        if user.share:
            task = request.env['project.task'].sudo().search(
                [('channel_id', '=', channel.id)], limit=1)
            if not task:
                return False
            return task._portal_partner_can_access(user_partner)
        return True
```

`_check_project_access`（:99）docstring 後、`if not project.cloud_service_id:` 前插入：

```python
        user = request.env.user
        if user.share:
            # Positive-only portal leg (two-layer-acl-lockstep Layer 2).
            # Placed FIRST so portal users can never reach the internal
            # fail-open branches below (F4 stays internal-scoped).
            return project.sudo()._portal_partner_can_access(user.partner_id)
```

（原 `user_partner = request.env.user.partner_id` 行保持不動，只在前面插入。）

`_check_task_access`（:116）`if not task.project_id:` 前插入：

```python
        if request.env.user.share:
            return task.sudo()._portal_partner_can_access(
                request.env.user.partner_id)
```

- [ ] **Step 4: 跑測試確認綠 + 既有測試迴歸**

Run: `--test-tags /woow_paas_platform:TestPortalAgentEndpoints` → 全 PASS
Run: `--test-tags /woow_paas_platform`（全模組）→ 不得出現新紅（特別注意 `test_ai_assistant.py`、`test_agent_status_endpoint.py`、`test_agent_pod_phase_c_sse_gateway.py`）

- [ ] **Step 5: Commit**

```bash
git add src/controllers/ai_assistant.py src/tests/test_portal_agent_endpoints.py
git commit -m "feat: portal-leg branches in AI controller access helpers with endpoint tests"
```

---

### Task 3: SSE stream 的 portal gate（HttpCase）

**Files:**
- Test: `src/tests/test_portal_agent_page.py`（新增，本 task 先放 SSE class，Task 5 再加頁面 class）

**背景**：`/api/ai/stream`（:442）入口先 csrf → `_check_channel_access`（Task 2 已涵蓋 portal）→ runtime gate → `_stream_agent_runtime` 內 `_check_project_access`（:659，Task 2 已涵蓋）。本 task 用行為探針驗證整條鏈：shared portal user 能「到達 runtime 分支」（error_code 非 access 類），unshared 被擋。csrf 取得手法沿用 `test_agent_pod_phase_c_sse_gateway.py:196` 的 `_csrf_token` pattern（portal user 用 `/my` 頁）。

- [ ] **Step 1: 寫失敗測試**

建立 `src/tests/test_portal_agent_page.py`：

```python
"""Portal SSE gate + portal page rendering (HttpCase)."""
import json
import re
import uuid

from odoo.tests.common import HttpCase, tagged

PASSWORD = 'Portal!234'


class PortalAgentFixture(HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        suffix = uuid.uuid4().hex[:8]
        cls.suffix = suffix
        Partner = cls.env['res.partner']
        User = cls.env['res.users']
        portal_group = cls.env.ref('base.group_portal')

        cls.partner_alice = Partner.create({
            'name': f'PG Alice {suffix}', 'email': f'pg-a-{suffix}@t.example'})
        cls.partner_bob = Partner.create({
            'name': f'PG Bob {suffix}', 'email': f'pg-b-{suffix}@t.example'})
        cls.alice = User.create({
            'login': f'pg_alice_{suffix}', 'name': 'Alice', 'password': PASSWORD,
            'partner_id': cls.partner_alice.id,
            'groups_id': [(6, 0, [portal_group.id])]})
        cls.bob = User.create({
            'login': f'pg_bob_{suffix}', 'name': 'Bob', 'password': PASSWORD,
            'partner_id': cls.partner_bob.id,
            'groups_id': [(6, 0, [portal_group.id])]})

        cls.project = cls.env['project.project'].create({
            'name': f'PG Proj {suffix}', 'privacy_visibility': 'portal',
            'use_agent_runtime': True})
        cls.task = cls.env['project.task'].create({
            'name': f'PG Task {suffix}', 'project_id': cls.project.id,
            'ai_auto_reply': False})
        cls.task.message_subscribe(partner_ids=[cls.partner_alice.id])
        cls.channel = cls.task._create_chat_channel()
        cls.channel.sudo().write({'channel_member_ids': [
            (0, 0, {'partner_id': cls.partner_alice.id}),
            (0, 0, {'partner_id': cls.partner_bob.id}),
        ]})

    def _csrf_token(self):
        for page in ('/my', '/web'):
            resp = self.url_open(page, timeout=15)
            if resp.status_code != 200:
                continue
            m = re.search(
                r'csrf_token[\"\']?\s*[:=]\s*[\"\']([^\"\']+)[\"\']', resp.text)
            if m:
                return m.group(1)
        self.fail('csrf token could not be extracted — fixture issue')

    def _first_sse_frame(self, resp):
        for line in resp.text.splitlines():
            line = line.strip()
            if line.startswith('data:'):
                return json.loads(line[len('data:'):].strip())
        return {}


@tagged('post_install', '-at_install')
class TestPortalSseGate(PortalAgentFixture):

    def test_shared_portal_reaches_runtime_branch(self):
        """Alice passes both gates; error (if any) is runtime-stage, not ACL."""
        self.authenticate(self.alice.login, PASSWORD)
        token = self._csrf_token()
        resp = self.url_open(
            f'/api/ai/stream/{self.channel.id}?csrf_token={token}', timeout=30)
        frame = self._first_sse_frame(resp)
        self.assertNotIn(frame.get('error_code'),
                         ('access_denied', 'project_access_denied',
                          'csrf_error', 'channel_not_found'),
                         frame)

    def test_unshared_portal_denied_at_channel_gate(self):
        self.authenticate(self.bob.login, PASSWORD)
        token = self._csrf_token()
        resp = self.url_open(
            f'/api/ai/stream/{self.channel.id}?csrf_token={token}', timeout=30)
        frame = self._first_sse_frame(resp)
        self.assertEqual(frame.get('error_code'), 'access_denied', frame)
```

- [ ] **Step 2: 跑測試**

Run: `--test-tags /woow_paas_platform:TestPortalSseGate`
Expected: Task 2 已落地 → 兩個 test 應直接 PASS（這是整鏈驗證、非新功能）。若紅，回頭修 Task 2 的分支，**不得**改測試遷就。

- [ ] **Step 3: Commit**

```bash
git add src/tests/test_portal_agent_page.py
git commit -m "test: portal SSE gate chain coverage (shared reaches runtime, unshared denied)"
```

---

### Task 4: Task context hook（簡單版，TDD）

**Files:**
- Modify: `src/models/project_task.py`（加 `_agent_task_context_for_channel`）
- Modify: `src/controllers/ai_assistant.py`（legacy `api_ai_stream` ~:514 後、runtime `_stream_agent_runtime` payload :769）
- Test: `src/tests/test_agent_task_context.py`（新增）

**背景**：會議 Action #3「接 task description」。**刻意極小**：`feat-enrich-agent-context` branch 正在做完整版，本 hook 之後由其收編（merge 協調點）。runtime payload 的 `task_context` 是 additive 欄位，pod 端是否消費屬 `paas-agent-runtime` repo 範圍——**不在本 plan**。

- [ ] **Step 1: 寫失敗測試**

建立 `src/tests/test_agent_task_context.py`：

```python
"""Minimal task-context hook (will be absorbed by feat-enrich-agent-context)."""
import uuid

from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestAgentTaskContext(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        suffix = uuid.uuid4().hex[:8]
        cls.project = cls.env['project.project'].create(
            {'name': f'Ctx Proj {suffix}'})
        cls.task = cls.env['project.task'].create({
            'name': f'Ctx Task {suffix}',
            'project_id': cls.project.id,
            'description': '<p>Fix the <b>billing</b> bug</p>',
        })
        cls.channel = cls.task._create_chat_channel()

    def test_context_contains_name_and_plain_description(self):
        ctx = self.env['project.task']._agent_task_context_for_channel(
            self.channel)
        self.assertIn(self.task.name, ctx)
        self.assertIn('Fix the billing bug', ctx)
        self.assertNotIn('<p>', ctx, 'description must be plain text')

    def test_context_empty_for_non_task_channel(self):
        ch = self.env['discuss.channel'].sudo().create({
            'name': 'free', 'channel_type': 'channel'})
        self.assertEqual(
            self.env['project.task']._agent_task_context_for_channel(ch), '')

    def test_context_truncates_long_description(self):
        self.task.description = '<p>' + ('x' * 5000) + '</p>'
        ctx = self.env['project.task']._agent_task_context_for_channel(
            self.channel)
        self.assertLessEqual(len(ctx), 2200)
```

- [ ] **Step 2: 跑測試確認紅**

Run: `--test-tags /woow_paas_platform:TestAgentTaskContext`
Expected: FAIL — `AttributeError: ... has no attribute '_agent_task_context_for_channel'`

- [ ] **Step 3: 實作 model method**

`src/models/project_task.py`（檔頭已有 `from odoo import api, fields, models`）class 內加：

```python
    @api.model
    def _agent_task_context_for_channel(self, channel):
        """Minimal task context (name + plain-text description) for the AI.

        Deliberately tiny: the full context-enrichment pipeline belongs to
        the ``feat-enrich-agent-context`` branch; when that lands this hook
        is expected to be absorbed by it (merge-coordination point — see
        docs/superpowers/plans/2026-06-05-portal-task-agent-chat.md).
        """
        from odoo.tools import html2plaintext
        task = self.sudo().search([('channel_id', '=', channel.id)], limit=1)
        if not task:
            return ''
        desc = html2plaintext(task.description or '').strip()
        if len(desc) > 2000:
            desc = desc[:2000] + '…'
        parts = [f'Current task: {task.name}']
        if desc:
            parts.append(f'Task description: {desc}')
        return '\n'.join(parts)
```

- [ ] **Step 4: 跑測試確認綠**

Run: `--test-tags /woow_paas_platform:TestAgentTaskContext` → 3 PASS

- [ ] **Step 5: 兩條 chat 路徑接線**

`src/controllers/ai_assistant.py` legacy 路徑 — 在 `api_ai_stream` 的 cloud_context 區塊（:512-516）後加：

```python
        # Minimal task context (feat-enrich-agent-context will absorb this).
        task_ctx = request.env['project.task']._agent_task_context_for_channel(
            channel)
        if task_ctx:
            system_prompt = (system_prompt + '\n\n' + task_ctx).strip()
```

runtime 路徑 — `_stream_agent_runtime` 內 `request_id = str(uuid.uuid4())`（:683）後加：

```python
        task_context = request.env['project.task'] \
            ._agent_task_context_for_channel(channel)
```

payload dict（:769-776）`'invoker_user_id': uid,` 後加一行：

```python
                            'task_context': task_context,
```

- [ ] **Step 6: 全模組迴歸 + Commit**

Run: `--test-tags /woow_paas_platform` → 無新紅

```bash
git add src/models/project_task.py src/controllers/ai_assistant.py src/tests/test_agent_task_context.py
git commit -m "feat: minimal task context hook for agent chat (legacy prompt + runtime payload)"
```

---

### Task 5: Portal page controller + QWeb templates（TDD, HttpCase）

**Files:**
- Create: `src/controllers/portal.py`
- Modify: `src/controllers/__init__.py`（加 `from . import portal`）
- Create: `src/views/portal_templates.xml`
- Modify: `src/__manifest__.py`（data list 加 `"views/portal_templates.xml"`，放在 `"views/paas_app.xml"` 之後）
- Test: `src/tests/test_portal_agent_page.py`（加 class）

**背景**：原生 hook `_task_get_page_view_values`（`odoo/addons/project/controllers/portal.py:280`）/ `_project_get_page_view_values`（:33）。頁面進得來代表已過 `_document_check_access`。channel ensure 用既有 `task._create_chat_channel()`（idempotent）。public（token 未登入）訪客不 join、不給 channel_id（前端顯示登入提示）。

- [ ] **Step 1: 寫失敗測試**

`src/tests/test_portal_agent_page.py` 文末加：

```python
@tagged('post_install', '-at_install')
class TestPortalAgentPage(PortalAgentFixture):

    def test_task_page_renders_chat_and_status_anchors(self):
        self.authenticate(self.alice.login, PASSWORD)
        resp = self.url_open(f'/my/tasks/{self.task.id}', timeout=20)
        self.assertEqual(resp.status_code, 200)
        self.assertIn('woow_paas_platform.PortalAgentStatus', resp.text)
        self.assertIn('woow_paas_platform.PortalTaskChat', resp.text)

    def test_task_page_auto_joins_portal_partner(self):
        # Carol: shared on task but NOT yet a channel member.
        suffix = self.suffix
        partner_carol = self.env['res.partner'].create({
            'name': f'PG Carol {suffix}', 'email': f'pg-c-{suffix}@t.example'})
        carol = self.env['res.users'].create({
            'login': f'pg_carol_{suffix}', 'name': 'Carol',
            'password': PASSWORD, 'partner_id': partner_carol.id,
            'groups_id': [(6, 0, [self.env.ref('base.group_portal').id])]})
        self.task.message_subscribe(partner_ids=[partner_carol.id])
        self.assertNotIn(partner_carol,
                         self.channel.sudo().channel_partner_ids)
        self.authenticate(carol.login, PASSWORD)
        resp = self.url_open(f'/my/tasks/{self.task.id}', timeout=20)
        self.assertEqual(resp.status_code, 200)
        self.assertIn(partner_carol,
                      self.channel.sudo().channel_partner_ids)

    def test_task_page_no_agent_block_when_runtime_disabled(self):
        self.project.use_agent_runtime = False
        self.authenticate(self.alice.login, PASSWORD)
        resp = self.url_open(f'/my/tasks/{self.task.id}', timeout=20)
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn('woow_paas_platform.PortalTaskChat', resp.text)
        self.project.use_agent_runtime = True

    def test_project_page_renders_status_anchor(self):
        self.project.message_subscribe(partner_ids=[self.partner_alice.id])
        self.authenticate(self.alice.login, PASSWORD)
        resp = self.url_open(f'/my/projects/{self.project.id}', timeout=20)
        self.assertEqual(resp.status_code, 200)
        self.assertIn('woow_paas_platform.PortalAgentStatus', resp.text)

    def test_unshared_portal_user_cannot_open_task_page(self):
        """Native portal access regression — Bob has no share at all."""
        self.authenticate(self.bob.login, PASSWORD)
        resp = self.url_open(f'/my/tasks/{self.task.id}', timeout=20)
        self.assertNotIn('woow_paas_platform.PortalTaskChat', resp.text)
```

- [ ] **Step 2: 跑測試確認紅**

Run: `--test-tags /woow_paas_platform:TestPortalAgentPage`
Expected: 前四個 FAIL（anchors 不存在 / 無 join）；`test_unshared_portal_user_cannot_open_task_page` PASS（原生行為）。

- [ ] **Step 3: 實作 controller**

建立 `src/controllers/portal.py`：

```python
"""Portal page extension: agent status card + AI chat on native pages.

Extends the native ``ProjectCustomerPortal`` view-value hooks. Pages are
reachable only after the native ``_document_check_access`` (ir.rule or
access_token), so values injected here add NO new visibility — the chat /
status data endpoints enforce their own portal legs (ai_assistant.py).
"""
import logging

from odoo.http import request

from odoo.addons.project.controllers.portal import ProjectCustomerPortal

_logger = logging.getLogger(__name__)


class PaasProjectCustomerPortal(ProjectCustomerPortal):

    def _task_get_page_view_values(self, task, access_token, **kwargs):
        values = super()._task_get_page_view_values(
            task, access_token, **kwargs)
        values.update(self._paas_agent_values_for_task(task))
        return values

    def _project_get_page_view_values(self, project, access_token, **kwargs):
        values = super()._project_get_page_view_values(
            project, access_token, **kwargs)
        values['paas_agent_enabled'] = bool(project.sudo().use_agent_runtime)
        values['paas_project_id'] = project.id
        return values

    def _paas_agent_values_for_task(self, task):
        """Chat/status view values + lazy channel provisioning.

        - Channel ensure reuses ``task._create_chat_channel()`` (idempotent).
        - The current (non-public) visitor is auto-joined to the channel —
          legitimate because this method only runs after the page passed
          ``_document_check_access``; revocation is enforced per-request by
          the controller portal legs, so stale membership carries no access.
        - Public (token-only, unauthenticated) visitors get no channel_id —
          the template renders a login hint instead (chat API is auth='user').
        """
        task = task.sudo()
        project = task.project_id
        values = {
            'paas_agent_enabled': bool(project and project.use_agent_runtime),
            'paas_project_id': project.id if project else False,
            'paas_channel_id': False,
        }
        if not values['paas_agent_enabled']:
            return values
        user = request.env.user
        if user._is_public():
            return values
        channel = task._create_chat_channel()
        partner = user.partner_id
        if partner not in channel.channel_partner_ids:
            channel.sudo().write({
                'channel_member_ids': [(0, 0, {'partner_id': partner.id})]})
        values['paas_channel_id'] = channel.id
        return values
```

`src/controllers/__init__.py` 加一行（依字母序插入既有 import 區）：

```python
from . import portal
```

- [ ] **Step 4: 實作 templates**

建立 `src/views/portal_templates.xml`：

```xml
<?xml version="1.0" encoding="utf-8"?>
<odoo>

  <!-- Task detail：status 卡片（原生空欄）+ chat 區塊（chatter 之前） -->
  <template id="portal_my_task_paas_agent"
            inherit_id="project.portal_my_task"
            name="Portal Task: PaaS Agent">
    <xpath expr="//div[@name='portal_my_task_second_column']" position="inside">
      <t t-if="paas_agent_enabled and paas_project_id">
        <owl-component name="woow_paas_platform.PortalAgentStatus"
                       t-attf-props='{"projectId": #{paas_project_id}}'/>
      </t>
    </xpath>
    <xpath expr="//div[@id='task_chat']" position="before">
      <div id="paas_task_chat" class="mt-4" t-if="paas_agent_enabled">
        <h5 class="mb-2">AI 助理對話</h5>
        <t t-if="paas_channel_id">
          <owl-component name="woow_paas_platform.PortalTaskChat"
                         t-attf-props='{"channelId": #{paas_channel_id}, "projectId": #{paas_project_id}}'/>
        </t>
        <div t-else="" class="alert alert-info mb-0">
          請先登入以使用 AI 對話。
        </div>
      </div>
    </xpath>
  </template>

  <!-- Project detail：status 卡片（searchbar 之前） -->
  <template id="portal_my_project_paas_agent"
            inherit_id="project.portal_my_project"
            name="Portal Project: PaaS Agent">
    <xpath expr="//t[@t-call='portal.portal_searchbar']" position="before">
      <t t-if="paas_agent_enabled and paas_project_id">
        <owl-component name="woow_paas_platform.PortalAgentStatus"
                       t-attf-props='{"projectId": #{paas_project_id}}'/>
      </t>
    </xpath>
  </template>

</odoo>
```

`src/__manifest__.py` data list：`"views/paas_app.xml",` 之後加 `"views/portal_templates.xml",`。

- [ ] **Step 5: 升級模組 + 跑測試確認綠**

升級（sandbox 內或本機 `-u woow_paas_platform --stop-after-init`），然後：
Run: `--test-tags /woow_paas_platform:TestPortalAgentPage` → 全 PASS

- [ ] **Step 6: Commit**

```bash
git add src/controllers/portal.py src/controllers/__init__.py src/views/portal_templates.xml src/__manifest__.py src/tests/test_portal_agent_page.py
git commit -m "feat: portal task/project page agent values, lazy channel join and QWeb anchors"
```

---

### Task 6: 共用 SSE consumer `ai_stream.js` + AiChat 重構

**Files:**
- Create: `src/static/src/paas/services/ai_stream.js`
- Modify: `src/static/src/paas/components/ai-chat/AiChat.js:192-383`（`startStream()`）

**背景**：SSE frame 協議（chunk / status:spawning / tool_* / warning / done / error）目前內嵌在 `AiChat.startStream`。抽出 consumer 讓 portal chat 共用（單一協議來源），AiChat 的狀態邏輯**原封不動**留在 handlers 內。`tool_unavailable` 原本就被忽略，維持。前端無 JS 單測設施 — 守線是既有 `test_agent_pod_phase_c_sse_gateway.py` SSE.2（URL contract regression）+ Step 4 的 /woow 手動驗證。

- [ ] **Step 1: 建立 `src/static/src/paas/services/ai_stream.js`**

```javascript
/** @odoo-module **/
import { aiService } from "./ai_service";

/**
 * Open the AI SSE stream for a channel and dispatch typed events.
 *
 * Shared protocol consumer for /woow AiChat AND the portal task chat —
 * single source of truth for the SSE frame contract:
 *   error / status(spawning) / tool_call / tool_result / tool_error /
 *   chunk / warning / done.  (`tool_unavailable` is intentionally ignored,
 *   matching the historical AiChat behaviour.)
 *
 * @param {number} channelId
 * @param {Object} handlers - optional callbacks:
 *   onError(data), onSpawning(data), onToolCall(data), onToolResult(data),
 *   onToolError(data), onChunk(data), onWarning(data), onDone(data),
 *   onParseError(raw, err), onConnectionError(event)
 * @returns {EventSource} caller is responsible for closing it
 */
export function openAiStream(channelId, handlers) {
  const url = aiService.getStreamUrl(channelId);
  const es = new EventSource(url);
  es.onmessage = (event) => {
    let data;
    try {
      data = JSON.parse(event.data);
    } catch (err) {
      handlers.onParseError?.(event.data, err);
      return;
    }
    if (data.error) {
      handlers.onError?.(data);
      return;
    }
    if (data.type === "status" && data.payload?.stage === "spawning") {
      handlers.onSpawning?.(data);
      return;
    }
    if (data.type === "tool_call") {
      handlers.onToolCall?.(data);
      return;
    }
    if (data.type === "tool_result") {
      handlers.onToolResult?.(data);
      return;
    }
    if (data.type === "tool_error") {
      handlers.onToolError?.(data);
      return;
    }
    if (data.chunk) {
      handlers.onChunk?.(data);
    }
    if (data.warning) {
      handlers.onWarning?.(data);
    }
    if (data.done) {
      handlers.onDone?.(data);
    }
  };
  es.onerror = (event) => handlers.onConnectionError?.(event);
  return es;
}
```

- [ ] **Step 2: 重構 `AiChat.startStream()`**

`src/static/src/paas/components/ai-chat/AiChat.js`：

檔頭 import 區加：

```javascript
import { openAiStream } from "../../services/ai_stream";
```

把 `startStream()` 內 `this.eventSource = new EventSource(url);` 起到 `onerror` handler 結尾（:207-382）整段換成（**所有 handler 內文 = 原 onmessage 各分支原文搬移，狀態邏輯零更動**）：

```javascript
    this.eventSource = openAiStream(this.props.channelId, {
      onError: (data) => {
        this.state.error = ERROR_MESSAGES[data.error_code] || data.error;
        this.state.errorRetryable = data.retryable !== false;
        if (this.state.errorRetryable) {
          const toolCalls = this.state.streamingToolCalls.length
            ? [...this.state.streamingToolCalls]
            : null;
          if (this.state.streamingText || toolCalls) {
            const aiMsg = this._createAiMessage(
              this.state.streamingText,
              data.message_id,
            );
            if (toolCalls) {
              aiMsg.toolCalls = toolCalls;
            }
            this.state.messages.push(aiMsg);
          }
        }
        this.state.connectionState = "error";
        this.closeStream();
      },
      onSpawning: () => {
        this._reconnectAttempts = 0;
        this._consecutiveParseErrors = 0;
        this.state.connectionState = "spawning";
        this.state.error = null;
      },
      onToolCall: (data) => {
        this._reconnectAttempts = 0;
        this._consecutiveParseErrors = 0;
        if (this.state.connectionState !== "streaming") {
          this.state.connectionState = "streaming";
        }
        this.state.streamingToolCalls.push({
          id: ++this._toolCallCounter,
          tool: data.tool,
          args: data.args || {},
          result: null,
          expanded: false,
        });
        this.scrollToBottom();
      },
      onToolResult: (data) => {
        this._reconnectAttempts = 0;
        this._consecutiveParseErrors = 0;
        const tc = [...this.state.streamingToolCalls]
          .reverse()
          .find((t) => t.tool === data.tool && t.result === null);
        if (tc) {
          tc.result = data.result || "";
        }
        this.scrollToBottom();
      },
      onToolError: (data) => {
        this._reconnectAttempts = 0;
        this._consecutiveParseErrors = 0;
        const tc = [...this.state.streamingToolCalls]
          .reverse()
          .find((t) => t.tool === data.tool && t.result === null);
        if (tc) {
          tc.result = null;
          tc.error = data.error || "Tool execution failed";
        }
        this.scrollToBottom();
      },
      onChunk: (data) => {
        this._reconnectAttempts = 0;
        this._consecutiveParseErrors = 0;
        if (this.state.connectionState !== "streaming") {
          this.state.connectionState = "streaming";
        }
        this.state.streamingText += data.chunk;
        this.scrollToBottom();
      },
      onWarning: (data) => {
        this.state.error = data.warning;
      },
      onDone: (data) => {
        const toolCalls = this.state.streamingToolCalls.length
          ? [...this.state.streamingToolCalls]
          : null;
        const hadContent = this.state.streamingText || toolCalls;
        // #129 INVARIANT: 最終訊息以 done frame 的 full_response 為準
        // （streamingText 僅作 fallback）— 不依賴逐 delta。
        if (hadContent) {
          const aiMsg = this._createAiMessage(
            data.full_response || this.state.streamingText,
            data.message_id,
          );
          if (toolCalls) {
            aiMsg.toolCalls = toolCalls;
          }
          this.state.messages.push(aiMsg);
        }
        this.closeStream();
        this.state.connectionState = "connected";
        this._connectedTimer = setTimeout(() => {
          if (this.state.connectionState === "connected") {
            this.state.connectionState = "idle";
          }
        }, 2000);
        this.scrollToBottom();
      },
      onParseError: (raw, err) => {
        console.error("Failed to parse SSE data:", raw, err);
        this._consecutiveParseErrors++;
        if (this._consecutiveParseErrors >= 3) {
          this.state.error = "接收 AI 回覆時發生資料錯誤，請重新整理頁面。";
          this.state.connectionState = "error";
          this.closeStream();
        }
      },
      onConnectionError: (event) => {
        console.error("SSE connection error:", event);
        const hadContent = !!this.state.streamingText;
        const partialText = this.state.streamingText;
        this.closeStream();
        if (hadContent) {
          this.state.messages.push(
            this._createAiMessage(
              partialText + "\n\n⚠️ (回覆因連線中斷而不完整)",
            ),
          );
          this.state.error =
            "AI 回覆因連線中斷而不完整，您可以重新傳送訊息以取得完整回覆。";
          this.state.connectionState = "error";
        } else {
          this.state.connectionState = "reconnecting";
          this._scheduleReconnect();
        }
      },
    });
```

同時刪除原本 `const url = aiService.getStreamUrl(this.props.channelId);` 行（URL 組裝已移入 consumer）。`closeStream()`（:388）不變（`EventSource.close()` 介面相同）。

- [ ] **Step 3: SSE.2 contract 迴歸**

Run: `--test-tags /woow_paas_platform:TestPhaseCSseFlagAndFrontendContract`
Expected: PASS（`ai_service.js` 的 `getStreamUrl` 未動）

- [ ] **Step 4: /woow 手動驗證（sandbox）**

在 sandbox 開 `/woow#/ai-assistant/projects/<id>`，對任一 task 發訊息，確認：串流逐字、tool call 卡片、done 後訊息落地、中斷重連 — 行為與重構前一致。

- [ ] **Step 5: Commit**

```bash
git add src/static/src/paas/services/ai_stream.js src/static/src/paas/components/ai-chat/AiChat.js
git commit -m "refactor: extract shared SSE protocol consumer ai_stream.js from AiChat"
```

---

### Task 7: Portal OWL 元件（status + chat）+ assets_frontend

**Files:**
- Create: `src/static/src/portal/portal_agent_status.js` / `.xml` / `.scss`
- Create: `src/static/src/portal/portal_task_chat.js` / `.xml` / `.scss`
- Modify: `src/__manifest__.py`（assets 加 `web.assets_frontend`）

**背景**：Odoo 18 `public_components` registry（`web/static/src/public/public_component_service.js`，已在 `web.assets_frontend`）自動 mount QWeb 中的 `<owl-component name="..." props="...">`。`odoo.csrf_token` 在 frontend layout 可用（`webclient_templates.xml:26`）→ `aiService.getStreamUrl` 直接可用。`ai_service.js`（reactive+fetch）、`rpc.js`（純 fetch）、`markdown_parser.js`（window.marked/DOMPurify + OWL markup）皆 frontend-safe，**同檔進 frontend bundle、零搬移**。

- [ ] **Step 1: manifest assets**

`src/__manifest__.py` 的 `"assets"` dict 加：

```python
        "web.assets_frontend": [
            "woow_paas_platform/static/src/paas/lib/purify.min.js",
            "woow_paas_platform/static/src/paas/lib/marked.min.js",
            "woow_paas_platform/static/src/paas/services/rpc.js",
            "woow_paas_platform/static/src/paas/services/markdown_parser.js",
            "woow_paas_platform/static/src/paas/services/ai_service.js",
            "woow_paas_platform/static/src/paas/services/ai_stream.js",
            "woow_paas_platform/static/src/portal/**/*",
        ],
```

- [ ] **Step 2: 建立 `src/static/src/portal/portal_agent_status.js`**

```javascript
/** @odoo-module **/
import { Component, onMounted, onWillStart, onWillUnmount, useState } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { aiService } from "../paas/services/ai_service";

const POD_STATE_LABEL = {
  absent: "未啟動",
  spawning: "啟動中",
  active: "運行中",
  reaped: "閒置",
  unknown: "未知",
};
const POLL_MS = 10000;

export class PortalAgentStatus extends Component {
  static template = "woow_paas_platform.PortalAgentStatus";
  static props = { projectId: Number };

  setup() {
    this.state = useState({ agent: null, pod: null, error: null });
    this._timer = null;
    this._onVisibility = () => {
      if (document.hidden) {
        this._stopPolling();
      } else {
        this._refreshPod();
        this._startPolling();
      }
    };
    onWillStart(async () => {
      const res = await aiService.fetchAgentStatus(this.props.projectId);
      if (res.success) {
        this.state.agent = res.data;
      } else {
        this.state.error = res.error || "無法取得 Agent 狀態";
      }
    });
    onMounted(() => {
      this._refreshPod();
      this._startPolling();
      document.addEventListener("visibilitychange", this._onVisibility);
    });
    onWillUnmount(() => {
      this._stopPolling();
      document.removeEventListener("visibilitychange", this._onVisibility);
    });
  }

  get podLabel() {
    return POD_STATE_LABEL[this.state.pod?.state] || POD_STATE_LABEL.unknown;
  }

  get podStateClass() {
    const s = this.state.pod?.state;
    if (s === "active") return "text-bg-success";
    if (s === "spawning") return "text-bg-warning";
    return "text-bg-secondary";
  }

  async _refreshPod() {
    const res = await aiService.fetchAgentPodStatus(this.props.projectId);
    if (res.success) {
      this.state.pod = res.data;
    }
  }

  _startPolling() {
    if (!this._timer) {
      this._timer = setInterval(() => this._refreshPod(), POLL_MS);
    }
  }

  _stopPolling() {
    if (this._timer) {
      clearInterval(this._timer);
      this._timer = null;
    }
  }
}

registry
  .category("public_components")
  .add("woow_paas_platform.PortalAgentStatus", PortalAgentStatus);
```

- [ ] **Step 3: 建立 `src/static/src/portal/portal_agent_status.xml`**

```xml
<?xml version="1.0" encoding="UTF-8"?>
<templates xml:space="preserve">

  <t t-name="woow_paas_platform.PortalAgentStatus">
    <div class="o_woow_portal_agent_status card mb-3">
      <div class="card-body py-3">
        <h6 class="card-title d-flex align-items-center mb-2">
          AI Agent
          <span t-attf-class="badge ms-2 {{ podStateClass }}" t-esc="podLabel"/>
        </h6>
        <div t-if="state.error" class="text-muted small" t-esc="state.error"/>
        <t t-elif="state.agent">
          <div class="small text-muted" t-if="state.agent.template_name">
            Agent：<span t-esc="state.agent.template_name"/>
          </div>
          <div class="small text-muted" t-if="state.agent.model_name">
            AI Connection：<span t-esc="state.agent.model_name"/>
          </div>
          <div class="small text-warning"
               t-if="!state.agent.ready and state.agent.reason"
               t-esc="state.agent.reason"/>
        </t>
        <div t-else="" class="text-muted small">載入中…</div>
      </div>
    </div>
  </t>

</templates>
```

- [ ] **Step 4: 建立 `src/static/src/portal/portal_task_chat.js`**

```javascript
/** @odoo-module **/
import { Component, onMounted, onWillStart, onWillUnmount, useRef, useState } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { aiService } from "../paas/services/ai_service";
import { openAiStream } from "../paas/services/ai_stream";
import { parseMarkdown } from "../paas/services/markdown_parser";

export class PortalTaskChat extends Component {
  static template = "woow_paas_platform.PortalTaskChat";
  static props = { channelId: Number, projectId: Number };

  setup() {
    this.state = useState({
      messages: [],          // {id, isAi, authorName, html, date}
      input: "",
      sending: false,
      streaming: false,
      streamingText: "",
      spawning: false,
      ready: false,
      notReadyReason: "",
      error: null,
    });
    this.eventSource = null;
    this.listRef = useRef("messageList");

    onWillStart(async () => {
      const [status, history] = await Promise.all([
        aiService.fetchAgentStatus(this.props.projectId),
        aiService.fetchChatHistory(this.props.channelId),
      ]);
      if (status.success) {
        this.state.ready = !!status.data.ready;
        this.state.notReadyReason = status.data.reason || "";
      } else {
        this.state.notReadyReason = status.error || "Agent 狀態取得失敗";
      }
      if (history.success) {
        this.state.messages = history.data.map((m) => this._toMessage(m));
      } else {
        this.state.error = history.error || "無法載入對話歷史";
      }
    });
    onMounted(() => this._scrollToBottom());
    onWillUnmount(() => this._closeStream());
  }

  get streamingHtml() {
    return parseMarkdown(this.state.streamingText);
  }

  _toMessage(m) {
    return {
      id: m.id,
      isAi: m.is_ai,
      authorName: m.author_name,
      html: m.is_ai ? parseMarkdown(m.body) : m.body,
      date: m.date,
    };
  }

  async send() {
    const body = this.state.input.trim();
    if (!body || this.state.sending || this.state.streaming || !this.state.ready) {
      return;
    }
    this.state.sending = true;
    this.state.error = null;
    const res = await aiService.postMessage(this.props.channelId, body);
    this.state.sending = false;
    if (!res.success) {
      this.state.error = res.error || "訊息傳送失敗";
      return;
    }
    this.state.messages.push({
      id: res.data.id,
      isAi: false,
      authorName: res.data.author_name,
      html: res.data.body,
      date: res.data.date,
    });
    this.state.input = "";
    this._scrollToBottom();
    this._startStream();
  }

  onKeydown(ev) {
    if (ev.key === "Enter" && !ev.shiftKey) {
      ev.preventDefault();
      this.send();
    }
  }

  _startStream() {
    this._closeStream();
    this.state.streaming = true;
    this.state.streamingText = "";
    this.state.spawning = false;
    this.eventSource = openAiStream(this.props.channelId, {
      onSpawning: () => {
        this.state.spawning = true;
      },
      onChunk: (data) => {
        this.state.spawning = false;
        this.state.streamingText += data.chunk;
        this._scrollToBottom();
      },
      onDone: (data) => {
        const text = data.full_response || this.state.streamingText;
        if (text) {
          this.state.messages.push({
            id: data.message_id,
            isAi: true,
            authorName: "AI",
            html: parseMarkdown(text),
            date: new Date().toISOString(),
          });
        }
        this._closeStream();
        this._scrollToBottom();
      },
      onError: (data) => {
        this.state.error = data.error || "AI 回覆失敗";
        this._closeStream();
      },
      onConnectionError: () => {
        if (this.state.streamingText) {
          this.state.messages.push({
            id: null,
            isAi: true,
            authorName: "AI",
            html: parseMarkdown(
              this.state.streamingText + "\n\n⚠️ (回覆因連線中斷而不完整)"),
            date: new Date().toISOString(),
          });
        } else {
          this.state.error = "連線中斷，請重新傳送訊息。";
        }
        this._closeStream();
      },
    });
  }

  _closeStream() {
    if (this.eventSource) {
      this.eventSource.close();
      this.eventSource = null;
    }
    this.state.streaming = false;
    this.state.streamingText = "";
    this.state.spawning = false;
  }

  _scrollToBottom() {
    requestAnimationFrame(() => {
      const el = this.listRef.el;
      if (el) {
        el.scrollTop = el.scrollHeight;
      }
    });
  }
}

registry
  .category("public_components")
  .add("woow_paas_platform.PortalTaskChat", PortalTaskChat);
```

- [ ] **Step 5: 建立 `src/static/src/portal/portal_task_chat.xml`**

```xml
<?xml version="1.0" encoding="UTF-8"?>
<templates xml:space="preserve">

  <t t-name="woow_paas_platform.PortalTaskChat">
    <div class="o_woow_portal_chat card">
      <div class="card-body p-3">
        <div class="o_woow_portal_chat__messages mb-3" t-ref="messageList">
          <div t-if="!state.messages.length and !state.streaming"
               class="text-muted small text-center py-3">
            還沒有對話，輸入訊息開始。
          </div>
          <t t-foreach="state.messages" t-as="msg" t-key="msg_index">
            <div t-attf-class="o_woow_portal_chat__msg mb-2 {{ msg.isAi ? 'o_woow_portal_chat__msg--ai' : 'o_woow_portal_chat__msg--user' }}">
              <div class="small text-muted" t-esc="msg.authorName"/>
              <div class="o_woow_portal_chat__bubble" t-out="msg.html"/>
            </div>
          </t>
          <div t-if="state.spawning" class="text-warning small py-1">
            AI 啟動中，請稍候…
          </div>
          <div t-if="state.streaming and state.streamingText"
               class="o_woow_portal_chat__msg o_woow_portal_chat__msg--ai mb-2">
            <div class="small text-muted">AI</div>
            <div class="o_woow_portal_chat__bubble" t-out="streamingHtml"/>
          </div>
        </div>
        <div t-if="state.error" class="alert alert-warning py-2 small"
             t-esc="state.error"/>
        <div t-if="!state.ready" class="alert alert-info py-2 small mb-2">
          AI 助理尚未就緒<t t-if="state.notReadyReason">：<t t-esc="state.notReadyReason"/></t>
        </div>
        <div class="d-flex gap-2">
          <textarea class="form-control" rows="2"
                    placeholder="輸入訊息…（Enter 送出）"
                    t-model="state.input"
                    t-att-disabled="!state.ready or state.streaming"
                    t-on-keydown="onKeydown"/>
          <button class="btn btn-primary align-self-end"
                  t-att-disabled="!state.ready or state.sending or state.streaming"
                  t-on-click="send">送出</button>
        </div>
      </div>
    </div>
  </t>

</templates>
```

- [ ] **Step 6: 建立 SCSS**

`src/static/src/portal/portal_agent_status.scss`：

```scss
.o_woow_portal_agent_status {
  .badge {
    font-weight: 500;
  }
}
```

`src/static/src/portal/portal_task_chat.scss`：

```scss
.o_woow_portal_chat {
  &__messages {
    max-height: 420px;
    overflow-y: auto;
  }
  &__msg {
    &--user .o_woow_portal_chat__bubble {
      background: var(--bs-primary-bg-subtle, #e7f1ff);
    }
    &--ai .o_woow_portal_chat__bubble {
      background: var(--bs-light, #f8f9fa);
    }
  }
  &__bubble {
    border-radius: 0.5rem;
    padding: 0.5rem 0.75rem;
    overflow-wrap: anywhere;
    pre {
      white-space: pre-wrap;
    }
  }
}
```

- [ ] **Step 7: 升級模組 + 全模組測試**

升級後 Run: `--test-tags /woow_paas_platform` → 無新紅（Task 5 的 page 測試仍綠 — anchors 字串不受 JS 影響）。

- [ ] **Step 8: Commit**

```bash
git add src/static/src/portal/ src/__manifest__.py
git commit -m "feat: portal OWL components for agent status card and task chat"
```

---

### Task 8: Sandbox 端到端驗證（playwright-cli, --headed）

**Files:** 無程式碼變更（驗證 task；發現 bug 則回前面 task 修）

- [ ] **Step 1: 部署 sandbox**

```bash
./.claude/skills/k8s-dev-sandbox/scripts/create.sh --context docker-desktop
./.claude/skills/k8s-dev-sandbox/scripts/status.sh <sandbox-name>
```

- [ ] **Step 2: 準備資料（sandbox Odoo 後台或 odoo shell）**

1. 建 portal user（聯絡人 + 授予 portal 存取）。
2. 建 project（`privacy_visibility='portal'`、`use_agent_runtime=True`、綁 agent template — 平台 seed `agent_template_default` 應已 auto-point；若 operator 未設定，status 卡片會顯示「Operator 未設定」— 這仍是有效的 UI 驗證）。
3. 建 task 並用原生 Share（read）分享給 portal user（或 `task.message_subscribe`）。

- [ ] **Step 3: playwright-cli 驗證（UX 流程用 playwright-cli --headed，依 CLAUDE.md 慣例）**

驗證清單：

| # | 操作 | 預期 |
|---|---|---|
| 1 | portal user 登入 → `/my/tasks/<id>` | 頁面渲染：右欄 Agent status 卡片（名稱/AI Connection/狀態 badge）、Communication history 上方出現「AI 助理對話」區塊 |
| 2 | status 卡片 | 10 秒輪詢 `agent-pod-status`（DevTools network 確認；切到背景分頁暫停） |
| 3 | 輸入訊息送出 | user 訊息入列 → 串流逐字渲染（或「AI 啟動中」黃色提示後開始串流）→ done 後訊息落地、Markdown 正常（粗體/清單/code block） |
| 4 | `/my/projects/<id>`（project 分享情境） | searchbar 上方 Agent status 卡片 |
| 5 | 另一 portal user（未分享）開同 URL | 原生 404/redirect；直打 `/api/ai/chat/history`（該 channel_id）→ `Access denied` |
| 6 | `/woow` 內部使用者開同 task 的 chat | 看得到 portal user 的對話（per-task 共享串）、/woow 串流功能不退化（Task 6 迴歸） |

- [ ] **Step 4: 銷毀 sandbox**

```bash
./.claude/skills/k8s-dev-sandbox/scripts/destroy.sh <sandbox-name> --context docker-desktop
```

---

### Task 9: Follow-ups 與收尾

**Files:**
- Modify: `docs/tracking/followups/follow-ups.md`

- [ ] **Step 1: 加 follow-up 條目**

在 `docs/tracking/followups/follow-ups.md` 的 open 區段（依該檔既有格式）加：

```markdown
- **F-PORTAL-1**（design note）：portal user 走 chat 時 `_get_project_access_role`
  回 `'guest'`（無 project_access row）→ spawn payload `invoker_role='guest'`。
  design §4.3 矩陣定義 guest 為 read-only，但 controller/pod 目前皆未 enforce，
  portal chat 因此可用。若未來 pod 端開始 enforce invoke 矩陣，portal user 需要
  明確的 role 對映（或 project_access row 自動建立），否則 portal chat 靜默失效
  （同 gap-chain class 失敗模式）。來源：2026-06-05 portal-task-agent-chat plan。
```

- [ ] **Step 2: 最終驗證 + Commit**

Run: `--test-tags /woow_paas_platform` 全綠後：

```bash
git add docs/tracking/followups/follow-ups.md
git commit -m "docs: track F-PORTAL-1 portal invoker_role guest semantics"
```

- [ ] **Step 3: 收尾選項**

用 `superpowers:finishing-a-development-branch` skill：PR 目標 `develop`（repo 慣例），PR 描述 zh-tw。merge 後的 stg e2e 由 maestri agent "deploy-stg" 在 develop 跑 `/woow:deploy-stg`（**不在本 plan 內**）。

---

## 風險與協調（執行時注意）

1. **`feat-enrich-agent-context` merge 協調**：Task 4 的 hook 極小且有註解標記；該 branch 落地後由其收編。若執行期間該 branch 已 merge 進 develop，先 rebase 再判斷 hook 是否已被涵蓋（涵蓋則 Task 4 縮為「確認 + 跳過」並回報）。
2. **AiChat 重構（Task 6）是唯一動到 /woow 既有行為的點**：handler 內文必須是原文搬移；/woow 手動驗證（Task 6 Step 4 + Task 8 #6）不可省。
3. **`test_portal_agent_endpoints` Step 2 的紅綠分佈**：與預期不符（特別是 fail-open 路徑「碰巧」擋住或放行）→ 停下回報，不要改測試遷就（plan-gap-stop-and-report）。
4. **owl-component 在 portal 不 mount**：若 Task 8 #1 元件沒出現，檢查順序：(a) bundle 是否載入（page source 找 `portal_agent_status`）→ (b) `odoo.__session_info__` / OWL 啟動錯誤（console）→ (c) `public_component_service` 是否掃到 `<owl-component>`（name 大小寫須與 registry key 完全一致）。
5. **SSE 並發**：每條串流佔 1 Odoo worker（`agent-runtime-chat-architecture.md` §3.1）— portal 上線後的容量觀測點，非本 plan 變更。
6. **已知 spec 偏差（記錄在案）**：spec §安全模型 5「deny 與 not-found 統一錯誤訊息防列舉」— 既有 `/api/ai/*` endpoint 實際上區分 `Channel not found` / `Access denied`（spec 對現況的描述有誤）。統一化會動到 /woow 前端 `ERROR_MESSAGES` 對映與既有測試，超出本次最小範圍 → 維持現狀，列為輕微 ID-存在性洩漏（portal user 可探知某 channel id 存在但無權）。若要收口另開 follow-up。
7. **Retry 語意**：portal chat 串流失敗後 composer 即恢復可用（重送 = retry），無 /woow 的獨立 retry 按鈕 — spawning 進度與 backend timeout（connect 60s / spawn 90s）與 /woow 完全同源。
