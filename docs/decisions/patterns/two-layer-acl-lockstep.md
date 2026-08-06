# Pattern: 兩層 ACL lock-step（ir.rule + controller carve-out / 多 endpoint）必須對齊收緊或放寬

> 適用：同一 model 的 ACL 被**兩個以上的 enforcement layer** 守（典型：ir.rule + controller `_check_*_access` carve-out；或 paas.py vs ai_assistant.py 兩個 endpoint 各自有 `_resolve_*_access`）。任一層改 domain / carve-out / 收緊條件，**另一層必須同步改** — silent divergence 比未 fix 更糟（attacker 知道修哪邊、不修哪邊）。

## When to use

當下列條件之一成立：

1. **Model 既有 ir.rule + controller 層的 access check**（如 `@route` handler 內呼 `_check_project_access(project)` carve-out）
2. **Model 被多個 controller endpoint 暴露**（如 `paas.py` 與 `ai_assistant.py` 各有自己的 access resolver helper — `_resolve_project_access` / `_check_project_access`）
3. **Model 有 ORM-layer constraint 同時被 controller 層 manual query 守相同 invariant**（如 D5 既靠 `@api.constrains`，又被 endpoint `_update_support_project` 手動 query 擋）

**典型情境（本 codebase 真實 case，2026-05-21 cycle 4 [[mcp-url-redesign]]）**：

```
project.project + user-scope mcp_server
  │
  ├── ir.rule mcp_server_rule_workspace_user (dual-leg)
  │     leg 1: bound walk
  │     leg 2: unbound + message_partner_ids carve-out  ◄── Layer 1
  │
  ├── controllers/ai_assistant.py::_check_project_access
  │     if not project.cloud_service_id:
  │         return user_partner in project.message_partner_ids  ◄── Layer 2a
  │
  └── controllers/paas.py::_resolve_project_access
        if not workspace:
            if user_partner in project.message_partner_ids:
                return project, _SyntheticAccess(ROLE_OWNER)  ◄── Layer 2b
```

三層 enforcement 對 unbound project + non-follower internal user 必須**統一回 deny**（且回相同的「Project not found or access denied」防 id enumeration），否則攻擊者可繞單一未對齊層做 IDOR。

**不適用**：

- 只有 ir.rule 一層 enforcement（沒有 controller carve-out）— 直接靠 ORM `with_user()` 即可
- 兩層 ACL 設計**故意不對齊**（如 controller 比 ir.rule 更鬆給 admin override） — 此情境須在 docstring 明示 caveat 寫「兩層故意 drift」，但仍建議走本 pattern 對等收緊，admin override 走 sudo / group_system 走 ir.rule `admin_full` rule
- 兩層 ACL 是同一 model 但不同 surface（如 form view 與 API endpoint）— 屬於 view-level 配置與 ACL 是兩種機制，不在本 pattern scope

## What it looks like

### 1. 兩層 carve-out 對齊（unbound project follower check 為例）

**Layer 1（ir.rule）— leg 2 unbound carve-out**：

```xml
<record id="mcp_server_rule_workspace_user" model="ir.rule">
    <field name="domain_force">['&amp;',
        ('scope', '=', 'user'),
        '|',
        ('project_id.cloud_service_ids.workspace_id.access_ids.user_id', '=', user.id),
        '&amp;',
        ('project_id.cloud_service_ids', '=', False),
        ('project_id.message_partner_ids', 'in', [user.partner_id.id])
    ]</field>
</record>
```

**Layer 2a（ai_assistant.py controller）— mirror leg 2 in Python**：

```python
def _check_project_access(self, project):
    if not project.cloud_service_id:
        # Unbound project — aligned with ir.rule workspace_user leg 2 (OR-7 (c))
        user_partner = request.env.user.partner_id
        return user_partner in project.message_partner_ids
    # bound branch ...
```

**Layer 2b（paas.py controller）— same mirror in different endpoint**：

```python
def _resolve_project_access(self, project_id):
    project = request.env['project.project'].sudo().browse(project_id)
    workspace = project.cloud_service_id.workspace_id
    if not workspace:
        # Unbound or bound-but-no-workspace — follower carve-out
        # in lock-step with ai_assistant.py:_check_project_access
        user_partner = request.env.user.partner_id
        if user_partner in project.message_partner_ids:
            return project, _SyntheticAccess(role=ROLE_OWNER)
        return None, None  # deny → caller 統一回 'Project not found or access denied'
    # bound + workspace branch ...
```

### 2. Docstring 標 lock-step 承諾

```python
class _SyntheticAccess:
    """In-memory access record for unbound project carve-out.

    Issued only after the unbound carve-out in _resolve_project_access has
    already verified that the current user is a project follower — the role
    is granted to a scope-resolved user, not to anyone authenticated.

    Mirrors the follower carve-out in ai_assistant.py:_check_project_access;
    both layers must stay in lock-step. See [[two-layer-acl-lockstep]] pattern.
    """
```

### 3. Test 兩層獨立 lock

```python
# Layer 1 lock — ir.rule leg 2 walk
def test_workspace_member_cannot_read_unbound_project_external_mcp_if_not_follower(self):
    dave = self._create_workspace_member_non_follower(self.unbound_project)
    self.assertFalse(
        self.external_mcp.with_user(dave).exists(),
        "leg 2 unbound carve-out must reject non-follower"
    )

# Layer 2a lock — ai_assistant.py controller
def test_check_project_access_unbound_non_follower(self):
    result = self.ai_assistant._check_project_access(self.unbound_project)
    self.assertFalse(result, "controller carve-out must reject non-follower")

# Layer 2b lock — paas.py controller
def test_endpoint_paas_resolve_project_access_unbound_non_follower_update_denied(self):
    response = self._post_as(dave, '/api/support-projects/<unbound_pid>',
                              {'action': 'update', 'name': '...'})
    self.assertEqual(response['error'], 'Project not found or access denied',
                     "two-layer lock-step must extend to paas.py endpoint")
```

**至少 3 條 test method**（每層獨立鎖一條），缺任一條 partial-correct fix 可逃過 review。

## Why this pattern

### 為什麼不只靠 ir.rule

ir.rule 在 ORM 層守 — 對「`record.with_user(env.user).read()`」path 完美 enforce。但 **controller 用 `sudo()` 取得 recordset 後手動讀寫欄位／注入 row** 的 path **繞 ir.rule**（`sudo()` 那一刻 ir.rule 就退場）。

**本節收的判準是這個「形態」，不是「controller 裡有沒有出現 `.sudo()`」** — `.sudo()` 在 controller 層是常見且正當的寫法，光是出現 `.sudo()` 不構成本 pattern 的成員。抽出本 pattern 時的原始案例都在 `src/controllers/paas.py`（都是底線開頭的 helper method，不是 `@route` endpoint）：

- `paas.py:_resolve_project_access` 用 `request.env['project.project'].sudo().browse(project_id)` 後手動 walk `project.cloud_service_id.workspace_id` 找 access — sudo browse 後**ir.rule 不會 evaluate**
- `paas.py:_create_project_mcp_server` 用 `McpServer.sudo().create({...})` 注入 row — sudo create 後**ir.rule create direction 不會 evaluate**

**⚠ 這串與上方圖的判準不同，不要互相對數。** 圖列的 Layer 1 / Layer 2a / Layer 2b 是 **enforcement 層**；這串列的是 **sudo 繞過風險路徑**。最容易被誤放進這串的是圖裡 Layer 2a 的 `src/controllers/ai_assistant.py:_check_project_access` — 它的 portal leg 也寫了 sudo（`return project.sudo()._portal_partner_can_access(user.partner_id)`），但這個 sudo 的結果**直接就是授權判斷的答案**（helper 的布林值原封不動 return 出去），它是**執法點**不是繞過點，所以不在這串裡。

Controller 走 sudo 是合法設計（如要繞 user-context ACL 為了 audit log / system action / cross-workspace cron），但 sudo + 缺 manual access check = **silent IDOR**（attacker 知 ID 即可繞）。

**Lock-step 要求**：controller 在 sudo path 之前**必須**用 Python access helper 對齊 ir.rule domain 邏輯，所有 sudo path 後的 mutation / read **必須**先過 helper。

### 為什麼 silent divergence 比未 fix 更糟

具體 attack pattern（2026-05-21 cycle 4 HC-2 attack chain）：

```
Pre-fix state:
  Layer 1 (ir.rule):           leg 2 → require follower (after #51 收緊)
  Layer 2a (ai_assistant.py):  unbound → require follower (本 PR 同步收緊)
  Layer 2b (paas.py):          unbound → blanket ROLE_OWNER (PR #53 pre-existing)
                                ⬆ silent divergence
```

Attacker 探測：

1. `ai_assistant.py /api/support/projects` 對 victim unbound project ID → "Project not found or access denied"（Layer 2a 收緊）→ attacker 認為 IDOR 被擋
2. **`paas.py /api/support-projects/<pid>` 對同 ID** → Layer 2b 未收緊 → blanket ROLE_OWNER → update / read 全通
3. 進一步 `POST .../mcp-servers action=create` → 注入 user-scope MCP row 到 victim project

**比 pre-fix（兩層都 blanket allow）更糟的理由**：

- pre-fix 兩層都 leaky → 安全人員一眼看出問題，會優先列入 review queue
- partial fix（Layer 2a 收緊、Layer 2b 漏） → 表象上 IDOR 被修，但攻擊者只要切換 endpoint 就繞過。Security audit 看 ai_assistant.py 認為 ACL OK，不會 cross-check paas.py
- doc-code drift：Layer 2b 的 `_SyntheticAccess` docstring 寫「Aligns with ai_assistant.py」但實際已 drift → 後續 maintainer 信 docstring 不查 code

### 為什麼 controller helper 對齊不是 over-engineering

直覺反對「為什麼要在 controller 重複 ir.rule 的 domain 邏輯？難道不該全靠 ORM？」

**因為 controller 有 sudo 必要**：

- Cross-workspace cron 需要 sudo（user-context 跨 workspace 沒權）
- Audit log 寫入需要 sudo（user 不該寫自己 audit）
- System action / scheduled job 需要 sudo
- 統一錯誤訊息（防 id enumeration）需要 sudo browse 後手動 check（否則 ir.rule deny 與 not-exist 兩條 error message 區別 attacker 可探）

Controller helper 是「sudo path 的 carve-out 邊界 explicit Python expression」— ir.rule 走不到的地方，由 helper 守。**Pattern 的核心**是：carve-out Python expression 與 ir.rule leg 2 domain 是 **same predicate expressed in two languages**。

## Anti-patterns（後人改本區前 STOP）

| 反 pattern | 問題 |
|-----------|------|
| 「Layer 2 用 `if not workspace: return True` blanket allow（PR #53 pre-existing）」 | Silent IDOR — partial collapse of Layer 1 |
| 「兩層分別實作 follower check 但用不同欄位（如一邊 `message_partner_ids`、一邊 `create_uid`）」 | Effective domain drift — 某些 user 在 Layer 1 通、Layer 2 不通（或反之） |
| 「Layer 2 sudo browse + 直接 access 欄位、沒 helper」 | sudo bypass ir.rule，任何 caller 都可繞 |
| 「Layer 1 改 domain shape 但忘 update Layer 2 helper」 | Cycle 4 HC-2 root cause |
| 「Layer 2 docstring 寫「Aligns with X」但實際 drift」 | Doc-code rot — 後續 maintainer 信 doc 不查 code，drift 永遠不被發現 |
| 「Test 只測 Layer 1 不測 Layer 2 endpoint path」 | 後續 PR 改 Layer 2 不會 trip test → silent regression |

## See also

- [[mcp-url-redesign]] §Round 2 改動敘事 — HC-2 抽出本 pattern 的 attack chain reproducer
- [[two-layer-acl-asymmetric-deny]] — 不走本 pattern 會踩的 pitfall（partial fix 比 unfix 更糟的 attack vector trace）
- [[mcp-acl-6-rule-split]] — ir.rule 層 6-rule split design（Layer 1 的 domain shape 與 perm direction split）
- [[unbound-project-acl-dual-leg]] — ir.rule dual-leg domain（Layer 1 leg 2 unbound carve-out 的 shape）
- [[cross-workspace-task-data-leak]] — sub-model 對等性原則（不同 model 之間 ACL 對等 — 與本 pattern 「同 model 多層」對稱）
- 關鍵 code：
  - `src/security/ir_rules.xml` `mcp_server_rule_workspace_user` + `mcp_tool_rule_workspace_user` — Layer 1 dual-leg domain
  - `src/controllers/ai_assistant.py:_check_project_access` — Layer 2a unbound carve-out
  - `src/controllers/paas.py:_resolve_project_access` — Layer 2b unbound carve-out（HC-2 hotfix 對齊）
  - `src/tests/test_mcp_acl_unbound_isolation.py` — H 段 12-method matrix（含 Layer 1 / Layer 2a / Layer 2b 三層獨立 lock）
- 上游 Odoo 原理：
  - sudo() 只 bypass `ir.rule` 與 `ir.model.access`，不 bypass `@api.constrains` — 但 sudo path 後的 controller field access 完全繞 ir.rule
  - per-group rule OR semantics — 即使 ir.rule 對 base.group_user 收緊，sudo（superuser context）走 group_system 永真 leg
