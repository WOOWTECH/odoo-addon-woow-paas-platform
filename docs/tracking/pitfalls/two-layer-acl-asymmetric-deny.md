# Pitfall: 兩層 ACL 收緊一層 silent 漏另一層 — partial fix 比沒 fix 更糟

> Refactor 把 layer A（如 ir.rule + ai_assistant.py controller）的 unbound carve-out 從 blanket allow 收緊為 follower check；但 layer B（如 paas.py 另一個 endpoint）對同 model 同 surface 仍 blanket allow。**Attack surface 表象上縮窄一半，實際上攻擊者切換 endpoint 就繞過** — 比兩層都 leaky 更糟（兩層都 leaky 會被 audit 一眼看出；partial fix 容易讓 audit 認為 ACL OK）。

## Symptom

- ACL refactor PR 收緊 layer A 的 unbound carve-out（如 ir.rule leg 2 + ai_assistant.py `_check_project_access` 從 `return True` 改 follower check）
- Test suite layer A 路徑全綠（`test_check_project_access_unbound_non_follower` deny 成功）
- CAQA verdict APPROVED — 因為 CAQA 只 verify layer A 對 plan AC 對齊 + 沒分析 「同 model 別處 endpoint 是否也走 layer A 的收緊邏輯」
- **Reviewer multi-agent attack vector trace** 抓到 layer B 仍 blanket allow → 攻擊者可透過 layer B endpoint 對同 model 做 IDOR
- **Partial fix 比 unfix 更糟**：
  - Unfix state（兩層都 leaky）→ 安全 audit 一眼看出，會優先列入 review queue
  - Partial fix（layer A 收緊、layer B 漏） → 表象上 IDOR 被擋，layer A 的 test 全綠看似 secure；security audit 看 layer A 認為 ACL OK，不會 cross-check layer B
- **Doc-code drift 二次傷害**：layer B 的 `_SyntheticAccess` docstring 寫「Aligns with layer A」但實際已 drift → 後續 maintainer 信 docstring 不查 code

**本 case 的 surface（2026-05-21 cycle 4 HC-2）**：

```
state before cycle 4:
  Layer 1 ir.rule:           single-leg bound walk only (D5 守 unbound 不該存在)
  Layer 2a ai_assistant.py:  if not project.cloud_service_id: return True (blanket allow)
  Layer 2b paas.py:          if not workspace: return _SyntheticAccess(ROLE_OWNER) (blanket allow)

cycle 4 plan AC 動作:
  Layer 1: 加 dual-leg leg 2 (follower carve-out)
  Layer 2a: 改 follower check (AC #42 explicit)
  Layer 2b: 沒列在 AC 內 ◄── plan scope blindness

post-PR (pre-hotfix):
  Layer 1 ir.rule:           dual-leg ✓ (rule layer 守)
  Layer 2a ai_assistant.py:  follower check ✓ (lock-step)
  Layer 2b paas.py:          blanket ROLE_OWNER ◄── silent divergence

Reproducible attack chain:
  Dave (W2 user, 非 W1 unbound project follower) →
    POST /api/support-projects/<W1_unbound_pid>/mcp-servers action=create
    → paas.py:_resolve_project_access blanket ROLE_OWNER pass
    → McpServer.sudo().create({...}) bypass ir.rule leg 2
    → 注入 MCP row 到 victim unbound project
```

## Why it happens

### Plan AC scope blindness

Plan §Acceptance criteria AC #42 only 列 `ai_assistant.py:_check_project_access` 擴入 follower check：

> 42. `src/controllers/ai_assistant.py:_check_project_access`（line 43-61）unbound carve-out 從 `return True` 改：
>     ```python
>     if not project.cloud_service_id:
>         user_partner = request.env.user.partner_id
>         return user_partner in project.message_partner_ids
>     ```

但**沒列 `paas.py:_resolve_project_access` 對等改寫**。Plan 寫的 reasoning：「ACL **兩個 enforcement layer 不一致** → IDOR 仍 leak」— 但只把「兩個 layer」理解為「ir.rule + ai_assistant.py」，**沒列舉 paas.py 也是第三 layer**。

### CAQA verdict 對 plan AC 對齊 = 對「sub-surface 完整性」漏 verify

CAQA report §Plan alignment check 對 AC #42 verify「diff 顯示 ai_assistant.py 已改」→ 標 ✓。**但 CAQA 沒問**：「plan AC #42 列的 method 是不是 same-class endpoint 的唯一 entry？」

CAQA report line 190 處理 `/simplify` agent 提的「拒絕統一 `_resolve_project_access` vs `_check_project_access`」— 但**處理框架是 code style 問題**（agent 建議統一是 over-engineering），**不是 ACL security analysis**。

→ CAQA 的盲區：**只 verify plan AC 列舉的 method 對 implementation，沒 audit「同 model 同 surface 是否有其他 entry 走未收緊邏輯」**。

### `_SyntheticAccess` docstring 已 stale，但 CAQA 沒抓 doc-code drift

PR #53 引入 `_SyntheticAccess` 時 docstring 寫：

```python
class _SyntheticAccess:
    """Aligns with ai_assistant.py:_check_project_access: unbound projects
    grant global access. D5 ORM constraint still protects user-scope MCP integrity."""
```

cycle 4 動作：
- 收緊 ai_assistant.py → docstring 第一句 stale
- 拔 D5 → docstring 第二句 stale

但 cycle 4 CAQA 沒抓 docstring drift — comment-analyzer agent 在 reviewer phase 才抓到。**CAQA verify 框架是「invariant 對齊 code」，不是「doc-code 一致」**。

### 為什麼 attacker 容易發現

Pre-fix state 有「兩層都 leaky」signal — 安全 audit 看 ai_assistant.py + paas.py 兩 endpoint 都 blanket allow，自然 frame 為「ACL spec gap」優先列入 review。

Partial fix 後：

- ai_assistant.py 收緊 → 「IDOR fixed」 signal 強
- paas.py 漏掉 → 「same model 同 surface」signal 弱（需要 cross-endpoint trace 才能看出）

**Attacker 的探測模型**：

1. 對 victim resource ID 試 `/api/support/projects` endpoint → 401/「Project not found」→ 認為 IDOR 被擋
2. **切換 endpoint** 對 `/api/support-projects/<pid>` 試 → blanket pass → IDOR 仍生效

Attacker 知道「security fix 是 endpoint 級別」，比 defender 容易發現「**fix 是不是 endpoint scope 全 cover**」。

## How to avoid

### 1. Plan AC scope 列舉時 grep 同 model surface entry

每條 ACL refactor AC 必須列舉 **same-model 全部 entry path**：

```bash
# AC #42 預期改 ai_assistant.py:_check_project_access — 先 grep 全 codebase 找同 surface entry
$ grep -rn "project.*cloud_service_id.*IS NULL\|cloud_service_id\b.*=.*False" src/controllers/

# 預期回 hit：
src/controllers/ai_assistant.py:43-61    # _check_project_access carve-out (AC #42 已列)
src/controllers/paas.py:1496-1498        # _resolve_project_access carve-out ◄── missing AC
```

或更廣的 grep：

```bash
# 找所有 controller 對 unbound project 的 special handling
$ grep -rn "not project.cloud_service_id\|cloud_service_id\.workspace_id\|_SyntheticAccess\|blanket" src/controllers/
```

任何 hit 都必須在 plan AC 內 explicit 列舉「收緊」或「故意不收緊（caveat）」— 不可 silent 漏。

### 2. CAQA 框架 audit「same surface 完整性」（不只 plan AC 對齊）

CAQA verdict 框架除了「invariant 對齊 code」+「plan AC 對齊 implementation」之外，**加一條 audit step**：

> 對每條 ACL refactor AC，cross-reference grep 同 model surface entry — 列舉「在收緊 carve-out 範圍內」的 caveat / 「未在範圍內」的 follow-up。

CAQA 不該假設「plan AC 列出的就是全部」— **plan scope 與 ACL surface 完整性是兩件事**。

### 3. Reviewer multi-agent 對 silent-failure-hunter 拿 attack vector trace

Reviewer phase 必跑 `silent-failure-hunter` agent — 它的 attack vector trace 框架是：

1. 列 model 的 all read/write/create/unlink entry path
2. 對每條 entry 走「attacker explore: 從 caller 到 ORM mutation/read 中間有哪些 ACL gate」trace
3. 對所有 gate 找「same intent 跨 entry 是否對等」

[[mcp-url-redesign]] §Round 2 改動敘事「CAQA 漏抓的 3 條 hard caveat」記載：cycle 4 HC-2 是 silent-failure-hunter 抓到的，而非 CAQA。**這個 trace 在 CAQA 框架內不存在**（CAQA 不掃 cross-endpoint attack chain），只能靠 multi-agent review 補強。

### 4. 套 [[two-layer-acl-lockstep]] pattern — code 與 docstring 標明 lock-step 承諾

Layer B 的 carve-out helper docstring **必須** 明示「lock-step with layer A」：

```python
class _SyntheticAccess:
    """In-memory access record for unbound project carve-out.

    Mirrors the follower carve-out in ai_assistant.py:_check_project_access;
    both layers must stay in lock-step. See [[two-layer-acl-lockstep]] pattern.

    Issued only after the unbound carve-out in _resolve_project_access has
    already verified that the current user is a project follower — the role
    is granted to a scope-resolved user, not to anyone authenticated.
    """
```

未來 maintainer 改 layer A 一邊時，grep `lock-step` 找到 layer B 同 idiom 警告 — 改 ACL 一邊不可不改另一邊。

### 5. Test scaffolding 必加 layer B endpoint test（不只 layer A）

對 same-surface 多 endpoint 必須 explicit test 每個 endpoint：

```python
# test_mcp_acl_unbound_isolation.py H 段 12-method 完整 matrix

# Layer 1 lock (ir.rule)
def test_workspace_member_non_follower_cannot_read_unbound_project_external_mcp(self):
    ...

# Layer 2a lock (ai_assistant.py)
def test_check_project_access_unbound_non_follower(self):
    ...

# Layer 2b lock (paas.py) — HC-2 round 2 補
def test_endpoint_paas_resolve_project_access_unbound_non_follower_update_denied(self):
    """HC-2 attack chain step 1 — Dave POST /api/support-projects/<unbound_pid> action=update
    must be rejected (paas.py layer in lock-step with ai_assistant.py layer)."""
    response = self._post_as(self.dave, '/api/support-projects/{}'.format(unbound_pid),
                              {'action': 'update', 'name': 'attacker_update'})
    self.assertEqual(response['error'], 'Project not found or access denied',
                     "two-layer lock-step must extend to paas.py endpoint")
```

「H 段 12-method」設計意圖：每個 method 名稱對應一個 4 × 2 × 4 = 32 cell 中的 specific case — 缺哪 layer 的哪個 perm direction 立即從 test method 名單可見。

### 6. Migration / PR description 必標 known-asymmetry caveat

若某 layer 故意不對等（如保留 admin override） — PR description 必 explicit 標 caveat：

> Caveat: paas.py `_resolve_project_access` 對 bound-but-no-workspace edge case silent 收緊（比 ai_assistant.py 嚴）— 此 edge case 在實際 PaaS 平台幾乎不會發生（CAQA M2 follow-up 一併處理）。Doc-code drift 風險：`_SyntheticAccess` docstring「lock-step」承諾僅 cover unbound case，bound-no-workspace 是 known divergence。

讓未來 maintainer 看 PR description 即可知道「這層故意 drift，不是漏」— 預防 over-corrected fix 把 known caveat 順手「fix」掉。

## Related

- [[two-layer-acl-lockstep]] — 本 pitfall 的解法 pattern
- [[mcp-url-redesign]] §Round 2 改動敘事 + §Hard caveats HC-2 — 完整 attack chain reproducer + Dave fix-loop
- [[legacy-endpoint-scope-blindness]] — 同源 pitfall 的另一 variant（plan AC scope blindness 在 SSRF helper 上的反映）
- [[mcp-project-decouple]] §Round 2 改動敘事 — multi-agent review 補 CAQA 視角的 meta-takeaway（PR #47 + cycle 4 = 兩次驗證）
- [[cross-workspace-task-data-leak]] §Why it happens — 「assertion + audit 走獨立 cross-check 才補上」的同質 lesson
- Code 引用：
  - `src/controllers/paas.py:_resolve_project_access:1495-1519` — HC-2 hotfix 落地 follower check + docstring lock-step 承諾
  - `src/controllers/ai_assistant.py:_check_project_access:44-58` — AC #42 收緊 follower check
  - `src/security/ir_rules.xml` `mcp_server_rule_workspace_user` + `mcp_tool_rule_workspace_user` — dual-leg domain leg 2
  - `src/tests/test_mcp_acl_unbound_isolation.py:849-1060` `TestPaasEndpointUnboundNonFollower` — Layer 2b explicit endpoint regression lock（3 case）
- Reports：
  - `docs/tracking/caqa-report-2026-05-20-mcp-refactor.md` line 190 — CAQA 把同 surface 兩 helper 處理為 code style 問題，沒分析 silent IDOR 後果
  - `docs/tracking/reviewer-report-2026-05-20-mcp-refactor.md` §Hard caveats HC-2 — silent-failure-hunter 抓到的 attack vector trace
- 上游 ACL 原理：
  - Odoo sudo() bypass ir.rule + ir.model.access — controller sudo path 必須 manual access check 對齊 ir.rule domain，不可只靠 ORM ir.rule
  - Per-endpoint access resolver 是 controller-level idiom，不是 model-level — 多個 endpoint 不會自動共用 ACL 邏輯
