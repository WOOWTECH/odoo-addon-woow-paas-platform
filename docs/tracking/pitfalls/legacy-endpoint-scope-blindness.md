# Pitfall: Plan AC 只 cover 新 endpoint，legacy same-source code path 漏裝同層 security helper

> Refactor 新加 SSRF / sanitize / access helper 時，plan AC 列入新 endpoint（如 `_create_project_mcp_server`），但**舊 endpoint** (`_create_mcp_server` legacy） 同 root-cause 但 plan 沒明列 → implementation 只裝新端點 → CAQA 對 plan AC 對齊 verify ✓ → reviewer multi-agent grep 才抓到 legacy 端點 same-source path 完全沒裝 helper。

## Symptom

- Refactor PR 加新 security helper（如 `_validate_mcp_url` SSRF check / `_sanitize_helm_values` AI prompt sanitizer）
- Plan AC 列入新端點 call site：`_create_project_mcp_server` + `_update_project_mcp_server`
- Implementation 對齊 plan AC 落地 helper call ✓
- Test report cover 新端點正反 case ✓
- CAQA verdict §Plan alignment check 標 ✓（implementation 對齊 plan AC #30）
- **Reviewer code-reviewer agent grep frontend caller** 抓到舊端點 `cloud_service.js:587/612/638` 仍主動 call legacy `_create_mcp_server` / `_update_mcp_server` — 這兩個 endpoint **沒裝** `_validate_mcp_url` → 同 root-cause 但 PR 沒 fix
- **Attack surface**：workspace owner / admin 仍可透過 legacy endpoint create row with `url=http://169.254.169.254/...` → 後續 `action_sync_tools` / `action_test_connection` 主動發 outbound HTTP → SSRF 雲端 metadata

**本 case 的 surface（2026-05-21 cycle 4 HC-1）**：

```bash
$ grep -rn "_validate_mcp_url" src/
src/controllers/paas.py:1681  # _create_project_mcp_server — plan AC #30 ✓
src/controllers/paas.py:1719  # _update_project_mcp_server — plan AC #30 ✓

$ grep -rn "_create_mcp_server\|_update_mcp_server" src/controllers/
src/controllers/paas.py:1348  # _create_mcp_server — legacy service-centric endpoint
src/controllers/paas.py:1374  # _update_mcp_server — legacy service-centric endpoint
                              # 兩個都沒 call _validate_mcp_url ◄── HC-1

$ grep -rn "_create_mcp_server\|_update_mcp_server" src/static/src/paas/
src/static/src/paas/services/cloud_service.js:587  # frontend 主動使用
src/static/src/paas/services/cloud_service.js:612  # 同上
src/static/src/paas/services/cloud_service.js:638  # 同上
```

Legacy endpoint 不是 dead code — frontend 仍主動使用。

## Why it happens

### Plan AC 寫法視角偏向「新端點」

Plan §AC #30 寫法：

> 30. `_create_project_mcp_server` / `_update_project_mcp_server` 對 user-provided `url` payload，validate：
>     - 拒絕 `169.254.169.254`（cloud metadata IP，IPv4）
>     - ...

AC explicit 列了**新端點** method 名稱。**Implicit 假設**：新端點是新 surface，舊端點不變 → 不需動。

**但同 root-cause 不擋舊 surface**：
- 漏裝 helper 的攻擊面是「user-provided URL → stored row → outbound HTTP」
- 舊端點與新端點都是 user-provided URL 的 write entry
- root cause（缺 URL validation）是 model-level 而非 endpoint-level

→ plan AC 寫法 scope 過窄。

### Implementation 對 plan AC 字面執行 — 不擴 scope 是合理的

Implementation phase 守 plan scope 原則：

- 不主動擴 scope（避免 PR diff 膨脹 + reviewer 走查負擔）
- 對 plan AC 字面對齊（不會自己 grep 「同 root-cause 是否還有別處」）
- 若發現 same-source path 沒列在 AC，**會 surface 給 leader**，但不會自己 fix

implementation phase 對 cycle 4 HC-1 的處理：grep `_validate_mcp_url` 找 plan 列的兩個 call site，加上去 ✓。**沒往上 grep frontend caller 追蹤同 root cause path** — 這超出 implementation 自我認知的 scope。

### CAQA 對「plan AC 對齊 implementation」verify 也漏

CAQA report §Plan alignment check verify 框架：

| 拍板 | Implementation | 對齊? |
|------|---------------|------|
| AC #30 minimal SSRF check | `_validate_mcp_url` 在新端點兩處 | ✓ |

CAQA 對 AC #30 verify「新端點兩處 call ✓」。**但 CAQA 框架不問**：「same root-cause（URL validation） 是否 cover 全部 user-input URL write entry？」

→ CAQA 的盲區：**plan AC 對齊 verify 是 narrow scope，不 audit「helper 是否裝到所有 same-root-cause path」**。

### 為什麼 reviewer 才抓到

Reviewer multi-agent `code-reviewer` agent 的 grep 框架：

1. 新 helper（`_validate_mcp_url`）grep 全 codebase 找 call site
2. 對 user-input URL write path grep 找 entry
3. Cross-reference 兩個 result list — entry 沒對應 helper call → flag

這個 cross-reference 步驟 CAQA 不做。**Reviewer cross-reference 是 catch「same root-cause but unfixed」的 hot zone**。

[[mcp-url-redesign]] §Round 2 改動敘事「CAQA 漏抓的 3 條 hard caveat」記載：cycle 4 HC-1 是 code-reviewer 抓到的，而非 CAQA。

## How to avoid

### 1. Plan AC 寫法強制「same-root-cause grep」步驟

對加新 security helper 的 AC，plan 必須 explicit 列：

> AC #30 同步動作：在加 `_validate_mcp_url` 前 grep `src/controllers/` 找「user-provided url write path」，列舉所有 entry 並在本 AC 內標「裝 helper / 故意不裝（caveat）」。

```bash
# Plan 寫 AC 時跑：
$ grep -rn "params\.get('url')\|params\['url'\]\|vals\['url'\]\b" src/controllers/

# 或語意更廣：
$ grep -rn "url.*=.*params\|McpServer.*sudo\(\).create\|McpServer.*write" src/controllers/
```

對每個 hit 在 plan AC 明示「在本 PR 收緊 / 留 follow-up」— **不允許 silent miss**。

### 2. Implementation phase 加 grep step

對「加新 helper」類動作，implementation phase 在 落地 helper 之後**自己跑一次 grep**：

```bash
# Cross-check helper 覆蓋
$ grep -rn "<new_helper>" src/
$ grep -rn "<same_root_cause_pattern>" src/

# 比對兩個 list — 若 same-root-cause hit 沒對應 helper call → surface 給 leader
```

不擴 scope ≠ 不做 cross-check。**Implementation phase 自己 grep 不是擴 scope，是對 plan AC 完整性的最後一道 sanity check**。

### 3. CAQA 框架加「same root-cause audit」step

CAQA §Plan alignment check 不只 verify「AC ✓ implementation」，加：

> 對每條加 security helper 的 AC，CAQA 必須 grep same-root-cause path 列舉「在 plan 範圍內」+「留 follow-up」+「漏列 outside scope」三類；任何屬於第三類 = MAJOR caveat surface 給 reviewer。

把「same root-cause but unfixed」從 reviewer 視角下移到 CAQA — 減輕 reviewer multi-agent 負擔。

### 4. Reviewer phase 必跑 code-reviewer agent grep + frontend caller 追蹤

Reviewer phase 對 ACL / SSRF / sanitize 類 PR 必跑 `code-reviewer` agent，並要求 agent：

1. grep 新 helper 全 codebase
2. 找 same-root-cause entry path（不一定 helper 名稱字面 — 走 semantic similarity）
3. 追 frontend caller（如 `src/static/src/paas/services/*.js`）verify legacy endpoint 不是 dead code
4. cross-reference 列「漏裝」cases

[[mcp-url-redesign]] §Round 2 改動敘事「為什麼 multi-agent review 對單一 reviewer (CAQA) 視角的補強價值」— 本 pitfall 是其具體 instance。

### 5. PR description 加 «same-root-cause 列表»

PR description 對「加 security helper」必含：

```markdown
## Same-root-cause coverage

Helper `_validate_mcp_url` 安裝在：
- ✓ `_create_project_mcp_server`（plan AC #30）
- ✓ `_update_project_mcp_server`（plan AC #30）
- ✓ `_create_mcp_server` legacy（HC-1 hotfix round 2）
- ✓ `_update_mcp_server` legacy（HC-1 hotfix round 2）
- (留 follow-up) admin form view backend write path — admin override，由 admin_full ACL 規範
- (留 follow-up) demo data XML — bypass record rule by design

未來 maintainer 加新 user-input URL write entry 必須 call _validate_mcp_url — 否則重炸 HC-1。
```

讓 reviewer / future maintainer 對「coverage 全完」明示，避免 silent gap。

### 6. 標 invariant 在 helper docstring

```python
def _validate_mcp_url(url):
    """Validate user-provided MCP URL before write.

    INVARIANT: any path that writes `mcp_server.url` from user-controlled
    input MUST call this validator before commit. Affected entry points:
    - paas.py:_create_mcp_server (legacy service-centric)
    - paas.py:_update_mcp_server (legacy service-centric)
    - paas.py:_create_project_mcp_server (project-centric)
    - paas.py:_update_project_mcp_server (project-centric)

    Admin form view backend write bypasses by design (admin_full ACL).
    Demo data XML bypasses record rule by design.

    Adding a new user-input URL write entry without calling this validator
    re-opens HC-1 SSRF surface. See [[legacy-endpoint-scope-blindness]].
    """
```

未來 maintainer grep 找 `_validate_mcp_url` 一見 docstring 就知道「全部 entry」邊界。

## Related

- [[mcp-url-redesign]] §Round 2 改動敘事 + §Hard caveats HC-1 — 完整 attack vector + fix-loop story
- [[two-layer-acl-asymmetric-deny]] — 同源 pitfall 的另一 variant（plan AC scope blindness 在 ACL carve-out 上的反映）
- [[mcp-project-decouple]] §Round 2 改動敘事 §3「同源 code path 檢查必須 walk 全 unlink/delete path」— 同 pattern 的 cleanup hook 版（PR #47 lesson）
- [[cross-workspace-task-data-leak]] §Hardening declaration ↔ migration 1:1 對稱 — 對 ir.rule declaration 與 migration 同步的 same-root-cause grep idiom
- Code 引用：
  - `src/controllers/paas.py:1353` (`_create_mcp_server`) — HC-1 hotfix `_validate_mcp_url` call
  - `src/controllers/paas.py:1387-1390` (`_update_mcp_server`) — HC-1 hotfix partial-update gating
  - `src/controllers/paas.py:1681` (`_create_project_mcp_server`) — plan AC #30 原始 call
  - `src/controllers/paas.py:1719` (`_update_project_mcp_server`) — plan AC #30 原始 call
  - `src/static/src/paas/services/cloud_service.js:587/612/638` — frontend 主動使用 legacy endpoint（verify「不是 dead code」）
  - `src/tests/test_mcp_external_url_creation.py:343-568` `TestLegacySsrfCheckOnMcpUrlCreate` — 4 test 鎖 legacy endpoint SSRF gate
- Reports：
  - `docs/tracking/caqa-report-2026-05-20-mcp-refactor.md` §Security 重點 3 — CAQA 只 verify 新端點 SSRF coverage，沒 grep legacy 端點
  - `docs/tracking/reviewer-report-2026-05-20-mcp-refactor.md` §Hard caveats HC-1 — code-reviewer agent 透過 grep + frontend caller 追蹤抓到
- 上游 codebase pattern：
  - PR #47 cycle 2 也踩過：`_sanitize_helm_values` 只裝在 `discuss_channel.py` non-streaming path，`ai_assistant.py` SSE streaming path 漏裝（F12 cluster）— 同 pattern 不同 helper
