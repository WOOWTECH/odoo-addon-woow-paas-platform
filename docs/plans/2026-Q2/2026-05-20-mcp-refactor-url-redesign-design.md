# Design: MCP user-scope URL 重設計 + sidecar / D5 invariant 重新審視 + unbound project ACL（合 #51）

**Date**: 2026-05-20
**Status**: design — signed off (2026-05-20)
**Issues**:
- [#49 refactor: 重新審視 sidecar MCP auto-create + D5 invariant + user-scope MCP URL 機制](https://git-prod.woowtech.io/odoo-addons/woow_paas_platform/issues/49)
- [#51 unbound project ACL（OR-7 / R-4 partial）](https://git-prod.woowtech.io/odoo-addons/woow_paas_platform/issues/51) — **合輪**（OQ-3 拍板 (a)，2026-05-20）
**Predecessors**:
- PR #47（`feature/project-mcp-skill` @ ebe8d67）— schema 解耦：`mcp_server.cloud_service_id → project_id`，產出 8 invariants
- PR #53（`feature/project-setting` → develop）— Project Detail page；OR-4 endpoint 手動 D5 query 為 cleanup debt；OR-7 unbound project ACL spec gap
**Cluster**: F22（OR-2 rebind race + OR-4 endpoint manual D5 query + OR-5 sidecar auto-create + R-4 model-layer `@api.constrains`） + F23（OR-7 unbound project ACL，合 #51）
**Companion plan**: `docs/plans/2026-Q2/2026-05-20-mcp-refactor-url-redesign-plan.md`
**Components**: `woow_paas_platform.mcp_server`, `woow_paas_platform.cloud_app_template`, `woow_paas_platform.cloud_service`, `controllers/paas.py`, `controllers/ai_assistant.py`（pending — surface to leader）, `extra/paas-operator/src/api/releases.py`（patch_sidecar / sidecar K8s Service / Cloudflare route）, demo data `cloud_app_templates.xml`, security `ir_rules.xml`（合 #51 後**動 6-rule split workspace_user dual-leg**）, frontend `McpServersSection`

> 本 design **不擴**：URL pattern 統一（#50 / F21）、frontend UX hardening（#52 / F25）。本 design 含 #49（F22 cluster）+ #51（F23 unbound project ACL），其餘 F1-F28 backlog 除 F22 / F23 外不處理。

## Context

PR #47 完成 schema 層 decouple — `mcp_server.project_id ondelete='cascade'`，AI chat 消費鏈走 `task.project_id.mcp_server_ids`。但**物理層**仍有兩條把 user-scope MCP 綁回 `cloud_service` 的繩子：

1. **Sidecar auto-create**（[[mcp-project-decouple]] §Inv #7 + Risk #4）— `cloud_service` 轉 `running` 時自動建 user-scope MCP record，URL 是 `http://{release}-mcp.{namespace}.svc.cluster.local:{port}{path}`（PR #47 Inv #7 文件記載；`cloud_service.py:1015-1036 _model_build_mcp_endpoint_url`）。MCP record 的「擁有者」物理上是 sidecar，schema 上掛 project。
2. **D5 invariant**（[[mcp-project-decouple]] §Inv #3） — `_check_user_scope_requires_bound_project`（`mcp_server.py:127-139`）禁止 unbound project 有 user-scope MCP。**根因**：URL 物理上依賴 sidecar；unbound project 無 service = 無 sidecar = URL 失效。

這兩條繩子讓三個後續問題無法收尾：

- **Issue #48 D2 / AC #22 措辭尷尬**（spec OR-5）— UI 必須中性化「不暗示 user-scope MCP 與 cloud_service 有 ownership 關係」，但實作層 sidecar auto-create 仍存在，UI 與 schema 各自承擔語意失真。
- **使用者無法手動加 external MCP**（issue 背景）— unbound project 創 user-scope MCP 直接 D5 violation。
- **Endpoint 手動 D5 query 是 cleanup debt**（spec OR-4 / R-4） — Project Detail page rebind / unbind endpoint 在 `paas.py:1530` 手動 query user-scope MCP 擋 unbind，因為 `@api.constrains('scope', 'project_id')` 不會在 `project.cloud_service_id` 變動時 fire（pre-existing ORM limitation）。

物理現實的延伸還有：

- **`auto_created=True` records 在 stg 已有產生**（assume — n8n template `is_active=True` + `mcp_enabled=True`，stg 任何部署 n8n 的 sandbox 都會 auto-create user-scope MCP）。Refactor 必須給這些 row 一條 migration path。
- **n8n template 的 AI 整合 value prop** — n8n template demo data（`cloud_app_templates.xml:89-100`）的 `ghcr.io/czlonkowski/n8n-mcp:latest` sidecar 是「n8n out-of-box AI 可用」的價值來源。任何拆 sidecar 的方案都會直接撞到這個 UX 承諾。

跨平面動作：Schema（拔 D5 / 改 `auto_created` 語意 / `cloud_app_template` 7 fields 去留）+ ORM（model-layer `@api.constrains` 收斂 OR-4 endpoint debt）+ Lifecycle（cleanup hook / unlink override step 1 評估）+ Helm/operator（sidecar patch_sidecar 是否仍呼叫）+ Demo data（n8n template）+ Frontend（McpServersSection 是否需 UI shortcut）。

## Goal

讓 user-scope MCP 在**物理層也屬於 project**（語意層 PR #47 已完成）。具體：

1. 拔 D5 invariant 或弱化為只剩 D4
2. 評估 sidecar auto-create 機制去留（全拆 / 半拆 / 拆為獨立 template）
3. 重設計 user-scope MCP URL 來源（A / B / C 三案）
4. 收斂 endpoint manual D5 query（OR-4） + 補 model-layer `@api.constrains`（R-4）
5. 給 rebind race condition（OR-2）一個設計層解
6. 處理既有 `auto_created=True` records migration

## URL 三方案探索

> 三方案直接對應 issue #49 描述的 A/B/C。下面以 5 persona 切角度，最後在「拍板建議」整合。

### 方案 A — 純 user-driven external URL

**核心動作**：
- 完全拆 sidecar 機制（n8n template `mcp_enabled` → `False`，operator `patch_sidecar` 不再被 Odoo 呼叫）
- `_model_auto_create_mcp_server` / `_auto_create_mcp_server` 全刪
- User-scope MCP 全部 user-driven create + user-provided URL
- D5 invariant 拔除（無物理依賴，無 invariant 可守）
- `cloud_app_template` 7 `mcp_*` fields 全刪
- `_build_mcp_sidecar_config` / `_model_build_mcp_endpoint_url` 全刪

**Persona 切角度**：

| Persona | 評估 |
|---|---|
| Security / threat-modeler | **SSRF surface 新加但不擴大** — `mcp_server.action_sync_tools` 已會對 user-supplied URL 發 HTTP（既有 system MCP 同樣 surface）。新 case：unbound external URL 可能走 `http://169.254.169.254`（雲端 metadata）/ `http://10.x.x.x`（cluster internal）/ `http://localhost:...`（pod loopback）— **需要加 URL scheme + host allowlist 驗證**作為新 security gate。Phishing 在 user 自家 AI chat 是 self-shoot，bounded。Cluster-internal URL 不再 leak 給 user（cluster.local URL 從產出端拔了），對 multi-tenant 微正面。 |
| UX | **HIGH cost**。n8n template「AI out-of-box 可用」的 value prop **直接消失**。User 必須自己 host n8n-mcp container / 配 auth token / 確認 cluster 內 Odoo runtime 能達到該 URL。Re-onboarding 既有 n8n user：要嘛配自己的 MCP，要嘛接受失去 AI 整合。Marketplace 描述要改（n8n 不再標榜「AI ready」）。 |
| Architect | **最 clean**。schema 與物理層完全 decouple，user-scope MCP 真的 100% 屬 project。沒有 sidecar lifecycle、URL 算法、D5 invariant、cleanup hook step 1、unlink override step 1、OR-2 race、OR-4 manual query、R-4 補 `@api.constrains` 都自動消失（沒有 invariant 可被 race / 可被 ORM 繞）。Cognitive load 降到最低。 |
| Migration | 既有 `auto_created=True` records 的 URL 立刻變 dead（sidecar 拆了 → cluster.local URL 物理失效）。三選項見下方「Migration 策略」。 |
| n8n template | **價值 prop 改寫**。Template 不再帶 mcp_* fields，AI 整合變 user opt-in 自架。 |

**Tradeoff matrix（量化）**：

| 維度 | 評分 |
|---|---|
| UX cost | 🔴 HIGH（n8n out-of-box AI 失能） |
| Security (SSRF) | 🟡 MEDIUM（需加 URL allowlist 新 gate） |
| Security (phishing) | 🟡 MEDIUM（user 自家 chat self-shoot） |
| Security (cluster URL exfil) | 🟢 LOW（cluster.local 不再產出） |
| Migration cost | 🟡 MEDIUM（既有 records 全部需處理） |
| Helm chart impact | 🟢 LOW（沒 chart 改動，只是 operator `patch_sidecar` 不被呼叫；K8s Service / Cloudflare route 自然不建） |
| n8n template impact | 🔴 HIGH（直接砍 mcp_* 價值） |
| Architectural cleanliness | 🟢 BEST（complete decouple） |
| 維護成本（後續） | 🟢 LOWEST |

---

### 方案 B — 保留 sidecar 但不自動建 MCP record（UI shortcut opt-in）

**核心動作**：
- Sidecar 物理機制全部保留（`patch_sidecar` 仍在 helm install 後跑、K8s Service `{release}-mcp` 仍建、Cloudflare route 仍建）
- `_model_auto_create_mcp_server` / `_auto_create_mcp_server` 全刪（**MCP record 不再自動建**）
- 新 UI：Project Detail page 的 McpServersSection 加「Add Sidecar MCP」按鈕，按下時自動偵測 bound service 的 sidecar、預填 cluster URL + auth token、user 確認後 commit 為 user-scope MCP record（`auto_created=True` 改 `sidecar_backed=True` 反映物理事實）
- D5 invariant 拔除（URL 不再強綁 sidecar 物理基礎；sidecar-backed 是 opt-in，user-provided external 是 opt-in，schema 不再強制 user-scope = sidecar）
- `cloud_app_template` 7 `mcp_*` fields **全 7 保留**（仍 driving sidecar 配置）
- `_build_mcp_sidecar_config` 保留
- `_model_build_mcp_endpoint_url` 可保留為 helper（UI shortcut 計算預填值時用），不做 auto-create 觸發
- `_do_cleanup` step 1（unlink user-scope MCP）+ `unlink()` override 行為改動：filter `sidecar_backed=True` 才清（sidecar 拆了，sidecar-backed MCP URL 失效；user-provided external MCP 不受影響保留）

**Persona 切角度**：

| Persona | 評估 |
|---|---|
| Security | **SSRF surface 新加但 default 預填值是 cluster-known-safe**（cluster.local URL）。User 可改 URL，與 A 案同需 allowlist。Cluster URL 預填給 user **不算 leak**：user 已是 workspace member、知道自己 release name/namespace；workspace ACL 已收緊（PR #47 6-rule split）。Phishing 與 A 案同 surface（user 改 URL 是 self-shoot）。 |
| UX | **LOWEST cost**。n8n out-of-box AI 整合**仍可一鍵 add**（按鈕預填值即可用）。比今天 auto-create 多一個 click，但換來 user 對「MCP 何時 active」有掌控（issue 描述的「使用者無法手動加 external MCP」也同樣 unblock — 同一 section 既能 add sidecar MCP 也能 add external MCP）。Marketplace 描述不變（n8n 仍「AI ready」）。 |
| Architect | **Hybrid**。schema 解耦完成（D5 拔），物理層仍有 sidecar 配套但**不自動 inject**。OR-4 / R-4 / OR-2 都自然消失（沒 invariant 可被 race / 可被 ORM 繞）。Cleanup hook step 1 / unlink override 邏輯**仍需保留**但 filter 改寫（`sidecar_backed=True` 才清 — `auto_created` 欄位語意微擴成「URL 由系統預填」flag，rename 為 `sidecar_backed` 或保留語意更清）。 |
| Migration | 既有 `auto_created=True` records：sidecar 仍在 → URL 仍 valid，行為**不變**。`auto_created` 欄位語意微擴或 rename。最低 migration cost。 |
| n8n template | **零影響**。7 mcp_* fields 仍 driving sidecar 配置；demo data 不動。 |

**Tradeoff matrix**：

| 維度 | 評分 |
|---|---|
| UX cost | 🟢 LOWEST（一鍵 add + 預填） |
| Security (SSRF) | 🟡 MEDIUM（同 A，需 allowlist） |
| Security (phishing) | 🟢 LOW（default 預填值安全） |
| Security (cluster URL exfil) | 🟡 MEDIUM（cluster URL 預填給 user，但 user 是 workspace owner 知道自己 release，不算 leak） |
| Migration cost | 🟢 LOWEST（既有 records 不變） |
| Helm chart impact | 🟢 LOW（無改動） |
| n8n template impact | 🟢 LOW（零改） |
| Architectural cleanliness | 🟡 MEDIUM（hybrid，sidecar 物理仍存在但不自動 inject MCP record） |
| 維護成本（後續） | 🟡 MEDIUM（hybrid 模型需 doc invariant；`sidecar_backed` flag 後續需 lifecycle 維護） |

---

### 方案 C — sidecar 拆成獨立 cloud_service template

**核心動作**：
- 新增 `cloud_app_template` 一筆「MCP Server for n8n」（或泛用 generic「External MCP Bridge」template）
- N8N template 拆走 7 mcp_* fields → 新 mcp template 自己有 sidecar 配置
- User deploy n8n → 另需 deploy「MCP Service」template，引用 n8n service 為 target
- 新增 schema relation: MCP service 知道 target 是哪個 n8n service（cross-service binding，**新概念**）
- D5 拆，但**改投另一條 invariant**：「user-scope MCP 必有 `cloud_service` backing 且該 cloud_service.template.category = mcp」
- `_model_auto_create_mcp_server` 改寫 — MCP service deploy 時 auto-create user-scope MCP record（指向自己 service 而非 target n8n service）
- `_build_mcp_sidecar_config` 變 mcp template 自己的 main container spec（不再是 sidecar 而是獨立 service main）
- Operator `patch_sidecar` 路徑可廢（mcp service 是普通 helm release）

**Persona 切角度**：

| Persona | 評估 |
|---|---|
| Security | MCP service 與 n8n service 透過 K8s NetworkPolicy 跨 namespace 通訊（assume 同 workspace 共 namespace 還好）。Auth token 在兩個 service 間如何協調是新 surface。Network egress（mcp service → n8n service） 在 cluster 內，不擴 SSRF。 |
| UX | **HIGH cost**：user 要 deploy 兩個 service、扛兩份 quota、扛兩份計費。「為什麼 MCP 跟 n8n 是分開的？」是 onboarding 疑問。Marketplace 變雙 entry。 |
| Architect | **架構複雜化**。Sidecar 概念消失（正面），但 cross-service binding 是**從未有的概念**，需引入新 schema relation `cloud_service.mcp_target_service_id`（或 M2O / extras）。D5 改投新 invariant，複雜度沒少。template inter-dependency 也是新概念（marketplace 要表達「MCP for X」必須在 X exists 時才能 deploy）。 |
| Migration | **WORST**。既有 `auto_created=True` records 對應的 n8n service 沒有獨立 MCP service。需要寫 migration script「為每個 n8n service auto-deploy mcp-service」— 但 helm deploy 是不可逆動作 + user 沒同意 + 涉及計費。實際做法：archive 既有 records + 提示 user 手動 deploy MCP service。 |
| n8n template | 7 mcp_* fields 從 n8n template 移除到新 mcp template；n8n template **變回純 n8n**。 |

**Tradeoff matrix**：

| 維度 | 評分 |
|---|---|
| UX cost | 🔴 HIGH-MEDIUM（雙 service / 雙計費 / onboarding 疑問） |
| Security (SSRF) | 🟡 MEDIUM（同 A/B，需 allowlist） |
| Security (phishing) | 🟢 LOW（service 內部 URL 自動產生） |
| Security (cluster URL exfil) | 🟢 LOW（同 B） |
| Migration cost | 🔴 HIGH（既有 records 無自動路徑） |
| Helm chart impact | 🟡 MEDIUM（新 chart 或 chart 修改） |
| n8n template impact | 🟡 MEDIUM（mcp_* fields 搬家） |
| Architectural cleanliness | 🟡 MEDIUM（sidecar 概念消失 但 cross-service binding 是新複雜度） |
| 維護成本（後續） | 🔴 HIGH（兩個 template 對齊維護 / cross-service relation lifecycle） |

---

### 三方案並列總結

| 維度 | A（純 user-driven） | B（sidecar 保留 + UI shortcut） | C（獨立 mcp template） |
|---|---|---|---|
| **UX cost** | 🔴 HIGH | 🟢 LOWEST | 🔴 HIGH-MEDIUM |
| **Security (SSRF)** | 🟡 MEDIUM | 🟡 MEDIUM | 🟡 MEDIUM |
| **Security (phishing)** | 🟡 MEDIUM | 🟢 LOW | 🟢 LOW |
| **Security (cluster URL exfil)** | 🟢 LOW | 🟡 MEDIUM | 🟢 LOW |
| **Migration cost** | 🟡 MEDIUM | 🟢 LOWEST | 🔴 HIGH |
| **Helm chart impact** | 🟢 LOW | 🟢 LOW | 🟡 MEDIUM |
| **n8n template impact** | 🔴 HIGH | 🟢 LOW | 🟡 MEDIUM |
| **Architectural cleanliness** | 🟢 BEST | 🟡 MEDIUM | 🟡 MEDIUM |
| **維護成本（後續）** | 🟢 LOWEST | 🟡 MEDIUM | 🔴 HIGH |

## 拍板建議：**B**（保留 sidecar + UI shortcut + 不自動建 MCP record）

**核心理由**：

1. **UX 摩擦最低**。n8n template 的「out-of-box AI 整合」價值 prop 不變，只是從「自動」改「一鍵 add」。issue 描述的兩個痛點同時 unblock：
   - 「拆 sidecar + n8n 失去自帶 MCP」（A 案問題）→ B 不會發生
   - 「使用者無法手動加 external MCP」→ 同一 section 同時支援 sidecar-backed + external URL
2. **Migration cost 最低**。既有 `auto_created=True` records 行為不變（sidecar 仍在，URL 仍 valid）。`auto_created` 欄位語意微擴（rename 或保留），不需 archive / delete 既有 row。
3. **Schema 解耦目標仍達成**。D5 invariant 拔得乾淨，OR-2 / OR-4 / R-4 / OR-5 全部自然消失（沒有「user-scope MCP 必有 bound project」這條 invariant 後，不需要 race lock、不需要 endpoint 手動 query、不需要補 `@api.constrains`、UI 不需中性化）。
4. **Architectural cleanliness 折衷**。schema layer 完全 decouple，物理 layer 仍有 sidecar 配套但**是 opt-in**。比 A 案少一份「徹底切斷」但多一份「實用主義」；比 C 案少一份「重新發明 cross-service binding」的複雜度。
5. **後續演進路徑 open**。若未來想徹底走 A 案（拆 sidecar 完全），可以從 B 案不破壞性退化：
   - 把 UI shortcut button 改 hidden
   - 把 demo n8n template `mcp_enabled=False`
   - 既有 sidecar-backed records 自然 fade out（user 也可以選擇用 external URL）

**唯一 caveat**：B 案是 hybrid，cognitive load 比 A 高一點 — 後續 maintainer 看 `cloud_app_template.mcp_*` 仍存在 + `cloud_service._build_mcp_sidecar_config` 仍存在，但 user-scope MCP record 不再自動建 — 這個「物理仍 backed 但 schema 不 enforce」的 hybrid 必須在 invariant doc 寫清楚。本 design 在「Invariant 改動」段落直接 surface。

### 不選 A 的理由

- n8n out-of-box AI 整合是 marketplace 賣點，砍掉等於砍 n8n template 的核心 value prop。
- 既有 stg sandbox 的 n8n service 全部變 broken state，需要 user 行動才能恢復。
- 雖然 architectural cleanliness 最高，但「乾淨」不抵 UX regression。

### 不選 C 的理由

- 雙 service 模型在 marketplace 是 onboarding 反直覺，特別是 PaaS 主要客群是 SMB / 個人開發者。
- Cross-service binding 是從未有的概念，會引入新 schema relation + lifecycle invariant，本意是「decouple」反而加複雜度。
- Migration cost 最高，且 helm deploy 是不可逆 + 涉及計費，自動化 migration 不安全。

## 連帶決策（B 案落地）

對每個 cluster 內的問題在 B 案下答案：

### 1. `cloud_app_template.py:120-156` 的 7 mcp_* fields

**保留全 7**：
- `mcp_enabled` / `mcp_sidecar_image` / `mcp_sidecar_port` / `mcp_transport` / `mcp_endpoint_path` / `mcp_sidecar_env` / `mcp_api_key_helm_path`

理由：B 案下 sidecar 物理機制完整保留，這 7 個 field 仍 driving `_build_mcp_sidecar_config`（`cloud_service.py:671-735`）+ `_model_build_mcp_endpoint_url` URL 計算邏輯（後者改作為 UI shortcut 預填 helper，不做 auto-create 觸發）。

### 2. `cloud_service._do_cleanup` hook（line 1119-1174）step 1「unlink user-scope MCP」

**保留但 filter 改寫**：

```python
# 改前（PR #47 cleanup hook）
user_mcps = self.project_ids.mapped('mcp_server_ids').filtered(
    lambda s: s.scope == 'user'
)

# 改後（B 案）
user_mcps = self.project_ids.mapped('mcp_server_ids').filtered(
    lambda s: s.scope == 'user' and s.sidecar_backed  # 或保留 auto_created
)
```

理由：B 案下，user-scope MCP record 有兩種：
- **sidecar-backed**（系統預填 cluster.local URL，物理依賴 service 的 sidecar）— service 刪了 → sidecar 拆了 → URL 失效，需要清掉避免 zombie row
- **user-provided external**（user 手動填外部 URL，物理獨立於 service） — service 刪了 → URL 仍 valid → **不應清掉**，user 可能想保留這個 MCP 給其他用途

Filter 改寫後既保留 PR #47 BLOCKER 2 fix 的「sidecar 拆時 cleanup」精神，又不誤殺 external MCP record。

### 3. `cloud_service.unlink()` override（line 1176-1201）catch-all guard

**保留但 filter 改寫**：同 #2，把 `lambda s: s.scope == 'user'` 加上 `s.sidecar_backed`（或 `auto_created`）filter。

理由：與 #2 對齊，避免外部 URL MCP 在 service 直接 unlink path 被誤殺（[[cloud-service-unlink-override-cleanup]] pattern 仍適用，filter 條件擴一個維度）。

### 4. `paas.py:1530` endpoint manual query D5 violation check（OR-4 cleanup debt）

**移除**：直接刪掉 `_update_support_project` 內 unbind 時 query user-scope MCP 的整段邏輯（line 1528-1547）。

理由：B 案下 D5 invariant 拔除，unbind project 不再有任何 invariant 違反風險。endpoint 不需要再手動 query；ORM `project.write({'cloud_service_id': False})` 直接 commit 就好。

### 5. `project.project.cloud_service_id` model-layer `@api.constrains`（R-4）

**不補**：B 案下 D5 拔了，**沒有 invariant 可守**。`@api.constrains` 也就不需要新增。

理由：R-4 原意是想用 ORM-layer enforcement 收斂 OR-4 endpoint debt。B 案下 OR-4 直接消失（連 invariant 都沒了），R-4 也自動消失。後人若 PR 想加 user-scope MCP 與 project / service 的 binding constraint，由那次 PR 自己評估 — 本 PR scope 不涵蓋。

### 6. Rebind race condition（OR-2）設計層處理

**不需處理**：B 案下沒有「rebind 觸發 D5 violation」的 race，因為 D5 不存在。

技術細節：OR-2 原情境是「A 正在 rebind project to service X，B 同時 delete X」— 在 PR #47 schema 下，這會讓 user-scope MCP 變成 `project.cloud_service_id IS NULL` 狀態（D5 violation）。B 案下這個狀態合法：
- 若 user-scope MCP 是 sidecar-backed，cleanup hook 已在 service unlink 時清掉
- 若 user-scope MCP 是 external，URL 物理獨立，不受 service unlink 影響

任何「rebind 中途 service 被刪」場景結果是合法狀態。OR-2 race 自然消失。

## Migration 既有 `auto_created=True` records 策略（B 案）

**選 c（保留 + URL 變 fillable + flag rename）**：

- 不 archive（既有行為對 user 是 functional 的，archive 會 surprise user）
- 不 delete（不可逆 + 既有 row 對 AI chat 仍 valid）
- **Rename or 擴語意**：把 `auto_created=True` 改名或新增 `sidecar_backed=True`（boolean field on `mcp_server`）。Migration backfill: existing `auto_created=True` records → `sidecar_backed=True`（一條 SQL UPDATE）。
- URL 變 user-fillable（form view + endpoint accept `url` write payload；今天 view 已開放編輯，無需動）。
- 行為不變：sidecar 仍在 → cluster URL 仍 valid → AI chat tool call 仍 work。

對 A / C 方案的對應建議（供使用者比較）：

| 方案 | 既有 records 建議 |
|---|---|
| **A** | **a + b 混合**：先 archive 給 user 1-2 sprint 時間改 external URL；之後再批次 delete。**不可直接 b**：user 沒被 onboard 就刪 row 會 silent break AI chat。 |
| **B** | **c**：URL 變 fillable + flag rename。零 disruption（推薦）。 |
| **C** | **a + 人工 follow-up**：archive 給 user，附文案說明「請另外 deploy MCP service template」。**a 而非 b 因為 user 可能想保留 MCP record 配置給新 mcp service binding 用**。**自動 deploy mcp-service migration 不做**（不可逆 + 計費）。 |

## Invariant 改動敘事（PR #47 8 條 → B 案落地）

PR #47 落地 8 條 invariants（見 [[mcp-project-decouple]] §Invariants）。B 案動作如下：

| Inv # | 主旨 | B 案動作 | 為什麼 |
|---|---|---|---|
| **#1** | `mcp_server.project_id` 維持 `ondelete='cascade'` | **不動** | Project 刪 → user-scope MCP cascade 仍是 desired behavior，不論 MCP 是 sidecar-backed 或 external |
| **#2** | D4: `scope='user' iff project_id IS NOT NULL` | **不動** | D4 是純語意 invariant，與物理層無關，B 案不改 |
| **#3** | **D5: user-scope MCP 必有 bound project** | **拔除**（本 design 核心動作） | URL 不再強綁 sidecar 物理基礎；user-scope MCP 可掛 unbound project（external URL 場景） |
| **#4** | `_do_cleanup` 順序：unlink user MCP → helm uninstall → state | **保留但 filter 改寫**（連帶決策 #2） | sidecar-backed 仍需此順序；external MCP 不應被誤殺 |
| **#5** | mcp_server / mcp_tool 6-rule ACL split | **workspace_user + mcp_tool_workspace_user 對等改 dual-leg**（合 #51，OQ-3 拍板 (a)，OR-7 sub-decision 採用 message_partner_ids walk — 見「OR-7 三 options brainstorm」段） | 拔 D5 後 user-scope MCP 可掛 unbound project。workspace_user rule 改 dual-leg：**leg 1（bound project）**沿用原 walk `('project_id.cloud_service_id.workspace_id.access_ids.user_id', '=', user.id)`；**leg 2（unbound project carve-out）**新增 `'&', ('project_id.cloud_service_id', '=', False), ('project_id.message_partner_ids', 'in', user.partner_id.ids)`。對等動 `mcp_tool_rule_workspace_user`（domain 透過 `server_id.` 反向 — sub-model 對等原則 [[mcp-acl-6-rule-split]]）。`admin_full` 與 `system_readonly` 兩條不動 |
| **#6** | `mcp_server_rule_system_readonly` 設計 invariant | **不動** | 與 user-scope 拆綁無關 |
| **#7** | `mcp_server.url` 為 snapshot + 演算法物理歸 service | **演算法保留為 helper**（B 案下 UI shortcut 用它計算預填值，不做 auto-create 觸發） | sidecar 仍在 → URL 算法仍有用；不再強制 write，從「auto-create 寫入」轉「UI shortcut 預填」 |
| **#8** | system-scope MCP create 限 sudo / admin / demo data | **不動** | 與 user-scope 拆綁無關 |

**新 invariant（B 案落地後增加）**：

> **New Inv #9**：`mcp_server.sidecar_backed=True`（或保留 `auto_created` 語意擴張）的 user-scope MCP，必須在 `cloud_service._do_cleanup` 與 `cloud_service.unlink()` 兩條路徑被清掉。Filter 必須包含 `sidecar_backed=True`，**漏 filter** → external MCP 被誤殺（user 抱怨）；**漏整段 cleanup** → sidecar 拆後 cluster URL zombie（[[direct-unlink-bypass-cleanup-hook]] pitfall pattern 復活）。

> **New Inv #10**：UI shortcut「Add Sidecar MCP」按鈕觸發 create user-scope MCP 時，必須在 `mcp_server.sidecar_backed=True` 同時 write，否則 cleanup hook filter 不會清到，重新踩 zombie URL 坑。**enforcement 點**：endpoint `_create_project_mcp_server`（新增 path）寫入時必須帶 `sidecar_backed=True`；frontend McpServersSection 不能繞 endpoint 直接 ORM 寫。

## URL 算法新設計（B 案具體）

B 案下 URL 算法**幾乎不變**，但呼叫時機改：

```python
# Today (PR #47, _model_build_mcp_endpoint_url at cloud_service.py:1015):
# - 被 _model_auto_create_mcp_server / _auto_create_mcp_server 在 service 轉 running 時自動呼叫
# - 結果 snapshot 寫到 mcp_server.url

# B 案改：
# - _model_auto_create_mcp_server / _auto_create_mcp_server 全刪 — URL 不在 service 轉 running 時自動寫
# - _model_build_mcp_endpoint_url 改名 / 語意改為 "build sidecar URL hint"
# - 被 endpoint /api/support-projects/<id>/mcp-servers action=sidecar-hint 呼叫，回傳給前端 UI shortcut 預填
# - User 在 UI 確認後 commit，endpoint create 時帶 sidecar_backed=True

# URL 算法本身 unchanged:
#   if service.helm_release_name and service.helm_namespace:
#       return f"http://{release}-mcp.{namespace}.svc.cluster.local:{port}{path}"
#   elif service.subdomain:
#       return f"https://{subdomain}.{paas_domain}{path}"
#   else:
#       return f"http://localhost:{port}{path}"
```

新增 endpoint **建議** action（plan 內具體 spec，design 只列大方向）：

```
POST /api/support-projects/<int:project_id>/mcp-servers
action=sidecar-hint
→ {success: True, data: {url, transport, api_key, suggested_name}}
  // 從 project.cloud_service_id 找 service，呼叫 _build_mcp_endpoint_url helper 算 URL 與 transport
  // suggested_name = f"{service.name} MCP"
```

UI flow：

1. User 進 Project Detail page → McpServersSection
2. 點「Add Sidecar MCP」按鈕（**僅在 project 有 bound service 且 service.template.mcp_enabled=True 時顯示**）
3. Frontend 呼叫 `sidecar-hint` endpoint 拿預填值
4. 顯示 form modal（URL / transport / api_key 預填可改）
5. User 確認 → endpoint `action=create` 帶 `sidecar_backed=True` commit
6. 同時保留既有「Add External MCP」按鈕（不預填，user 自己填 URL）

兩個 button 並列，user 對「MCP 從哪來」有明確 mental model：
- **Sidecar MCP**：物理 backed by 該 project 的 cloud service sidecar → service 拆了會被 cleanup hook 清掉
- **External MCP**：物理獨立 → service 拆了仍存在

## Affected files（B 案）

### 變動文件（drop / rewrite）

| File | 動作 |
|---|---|
| `src/models/mcp_server.py:127-139` | **刪** `_check_user_scope_requires_bound_project` constraint（D5） |
| `src/models/mcp_server.py:93-98` | `auto_created` field rename to `sidecar_backed`（或保留 `auto_created` 並擴語意 — 由 implementation 階段定，本 design 偏好 rename 清楚） |
| `src/models/cloud_service.py:849, 914, 940` | **刪** 3 個 `_model_auto_create_mcp_server` call sites |
| `src/models/cloud_service.py:970-1013` | **刪** `_model_auto_create_mcp_server` 整個 method |
| `src/models/cloud_service.py:1015-1036` | `_model_build_mcp_endpoint_url` **保留**作為 helper（endpoint 呼叫），不被自動觸發 |
| `src/models/cloud_service.py:1119-1174` | `_do_cleanup` step 1 filter 改寫（加 `sidecar_backed=True`） |
| `src/models/cloud_service.py:1176-1201` | `unlink()` override filter 改寫（加 `sidecar_backed=True`） |
| `src/controllers/paas.py:2395, 2493, 2560` | **刪** 3 個 `_auto_create_mcp_server` call sites |
| `src/controllers/paas.py:2601-2660` | **刪** `_auto_create_mcp_server` method |
| `src/controllers/paas.py:2662-2696` | `_build_mcp_endpoint_url` **改名 + 改 expose** 為 `_compute_sidecar_url_hint`（被 sidecar-hint endpoint 呼叫） |
| `src/controllers/paas.py:1504-1556 (`_update_support_project`)` | **刪除** unbind 時 D5 manual query（OR-4 cleanup debt） |
| `src/controllers/paas.py` | **新增** `sidecar-hint` action handler on `/api/support-projects/<id>/mcp-servers` endpoint |

### 變動文件（合 #51 ACL — OQ-3 (a) 拍板）

| File | 動作 |
|---|---|
| `src/security/ir_rules.xml` `mcp_server_rule_workspace_user` | domain 改 dual-leg：leg 1 保留原 walk + leg 2 加 unbound carve-out `'&', ('project_id.cloud_service_id', '=', False), ('project_id.message_partner_ids', 'in', user.partner_id.ids)` |
| `src/security/ir_rules.xml` `mcp_tool_rule_workspace_user` | 對等改 dual-leg（前綴 `server_id.`）— sub-model 對等原則 [[mcp-acl-6-rule-split]] |
| `src/controllers/ai_assistant.py:_check_project_access` | unbound carve-out 從 `return True` 改 `return user_partner in project.message_partner_ids`（與 ir.rule leg 2 對齊）。**建議擴入，leader sign-off 時須拍板確認；若拒擴入，plan 須加 caveat「ACL 兩層不對齊，IDOR partial fix」** |

### 不動文件（保留物理機制 / 語意保留）

| File | 不動理由 |
|---|---|
| `src/models/cloud_app_template.py:120-156` | 7 mcp_* fields 全保留（sidecar 物理機制保留） |
| `src/models/cloud_service.py:478, 671-735` | `_build_mcp_sidecar_config` + `client.patch_sidecar` call 全保留 |
| `extra/paas-operator/src/api/releases.py:471-587 (patch_sidecar)` | Operator side 不動 |
| `extra/paas-operator/src/api/releases.py:611-705 (_create_sidecar_service_and_route)` | K8s Service + Cloudflare route 創建邏輯不動 |
| `extra/paas-operator/src/api/releases.py:231-244` | release 刪除時 sidecar Service + route 清理邏輯不動 |
| `src/data/cloud_app_templates.xml:89-100` (n8n template mcp_* fields) | demo 配置不動 |
| `src/security/ir_rules.xml` `mcp_server_rule_system_readonly` / `mcp_tool_rule_system_readonly` / `mcp_server_rule_admin_full` / `mcp_tool_rule_admin_full` | 4 條 system / admin rule **不動**（與 user-scope 拆綁無關，PR #47 Inv #6 / #8 仍 enforce） |
| `src/controllers/ai_assistant.py:_check_task_access` | task path IDOR (F4) 留 follow-up — 本 PR 只擴 project path |
| `src/controllers/ai_assistant.py:_list_projects` | sudo empty domain leak (F10) 留 follow-up — 獨立 attack surface |

### Frontend（plan 階段具體寫）

| File | 動作 |
|---|---|
| `src/static/src/paas/pages/project-detail/sections/McpServersSection.{js,xml}` | 新增「Add Sidecar MCP」按鈕（僅在 project bound service + template.mcp_enabled 時顯示）+ form modal pre-fill flow |
| `src/static/src/paas/services/support_service.js` | 新增 `fetchSidecarHint(projectId)` method |

### Migration

| File | 動作 |
|---|---|
| `src/migrations/18.0.1.0.25/post-migrate.py`（新檔） | (1) Backfill `sidecar_backed = auto_created`（或保留 field 名）；(2) D5 constraint drop 不需 SQL（drop constraint = remove from Python，no DB-level constraint）；(3) Drop orphan `auto_created=True` 但 `project_id.cloud_service_id IS NULL`（PR #47 archive 過的 row，本次可選擇 archive / 保留 — 推薦保留並讓 user 自己處理） |
| `src/__manifest__.py` | version bump `18.0.1.0.24` → `18.0.1.0.25` |

## OR-7 三 options brainstorm（合 #51 sub-decision）

**Resolved 2026-05-20 wave 2 → final pick (c) message_partner_ids walk**（OQ-8 user 拍板）。本 PR 與 #51 共 PR / 共 review。OR-7 (c) 對應 leg 2 落地到 ir.rule domain（AC #38 / #39）+ controller layer 對齊（AC #42，OQ-7 wave 2 拍板擴入）。

OQ-3 拍 (a) 合 #51 後，原本 #51 spec 內含「unbound project ACL」三 options 變成本 PR 內含的 sub-decision，必須在本 design 給出推薦並落地到 ir.rule domain。

### Three options

| Option | 機制 | Domain leg 2 表達 | Status |
|---|---|---|---|
| (a) reject all | unbound project 對所有 internal user 不可讀 | leg 2 = `[(1, '=', 0)]` (False)。等同沒拔 D5 — external MCP 在 unbound project 下不可用 → B 案核心需求破功 | rejected |
| (b) creator-only | 只有 `project.create_uid` 可讀 | leg 2 = `'&', ('project_id.cloud_service_id', '=', False), ('project_id.create_uid', '=', user.id)` | rejected |
| **(c) message_partner_ids walk** ✅ **final pick** | 與 Odoo std project follower 隔離邏輯對齊；creator 自動是 follower（`project_partner_ids` 預設含 create_uid.partner_id） | leg 2 = `'&', ('project_id.cloud_service_id', '=', False), ('project_id.message_partner_ids', 'in', user.partner_id.ids)` | **picked by user OQ-8 wave 2，2026-05-20** |

### Final pick (c) message_partner_ids walk — 拍板理由

**理由**：

1. **與既有 codebase pattern 一致**：[[cloud-service-project-decouple]] D3=A 已採 `message_partner_ids + create_uid` 雙腿做 unbound project ir.rule（cycle 1 Q1 拍板原因見 §Q1 「`user_ids` 不在 Odoo 18 `project.project` schema」）。本 PR 沿用同 idiom，maintainer mental model 不破。
2. **與 Odoo std project follower 隔離心智模型對齊**：std `project.project_public_members_rule`（雖然在本 instance 已 deactivate per [[cross-workspace-task-data-leak]]）也是走 follower 概念。Workspace 內 collaboration 自然透過 add follower 表達。
3. **Creator 自動 covered**：Odoo `project.project` 在 create 時自動把 `create_uid.partner_id` 加進 `message_partner_ids`（[[cloud-service-project-decouple]] Q1 細節） — 不需要 dual-leg `create_uid OR message_partner_ids` 寫法。
4. **Cross-workspace 仍 deny**：跨 workspace user 不在 `message_partner_ids` 內 → leg 2 false；同時 leg 1 也 false（cloud_service_id NULL）→ 整 rule false → 跨 workspace 看不到 unbound project 的 external MCP。隔離 invariant 保留。
5. **Add follower 是 explicit collaboration affordance**：workspace owner 想分享 unbound project 給其他 member，需要 explicit `message_subscribe`（明示動作），而非 implicit「同 workspace 即可見」。

**為什麼不選 (b) creator-only**：

- 限制太死。Workspace 內第二位 member 必須要 creator 親自 add 才能讀，且無法分享 — 等於 workspace 內 collaboration 退化。
- 與 [[cloud-service-project-decouple]] D3=A 既有 ir.rule **不一致**（D3=A 是 `message_partner_ids + create_uid` 雙腿，B 退化只剩 creator）。

**為什麼不選 (a) reject all**：

- 拒絕所有 unbound 訪問 → external MCP 在 unbound project 下整個失效 → 等於拒絕 issue #49 描述的「使用者無法手動加 external MCP」痛點。B 案核心需求破功。
- D5 雖被拔但邏輯上仍存在（external MCP only works under bound project）— hidden invariant，比明示 D5 更糟。

### 對 `ai_assistant.py:_check_project_access` 的延伸（OQ-7 拍板 YES，wave 2，2026-05-20）

[[follow-ups]] F4 + F23 早就 flag：`controllers/ai_assistant.py:43-61` 對 `project.cloud_service_id IS NULL` 直接 `return True` 跳過 access check（pre-existing IDOR）。

本 PR 若**只**改 ir.rule 不改 controller check：

- ir.rule layer：leg 2 用 message_partner_ids walk，正確
- controller layer：`_check_project_access` 對 unbound 仍全放行 → 攻擊者透過 `api_support_projects(action='get', ..., project_id=X)` 對任意 unbound project ID 仍可讀
- ACL **兩個 enforcement layer 不一致** → IDOR 仍 leak（worse than 沒改 — 假象修好）

**拍板（OQ-7，user wave 2，2026-05-20）**：擴入 — 本 PR scope **minimal 擴入** `controllers/ai_assistant.py:_check_project_access` 對齊。具體：

```python
# _check_project_access (今天 line 43-61):
def _check_project_access(self, project):
    if not project.cloud_service_id:
        return True  # ← F4 pre-existing IDOR

# 改為（與 ir.rule leg 2 對齊）:
def _check_project_access(self, project):
    if not project.cloud_service_id:
        # Unbound project — same domain as ir.rule leg 2 (message_partner_ids walk)
        user_partner = request.env.user.partner_id
        return user_partner in project.message_partner_ids
```

對應 task 也類似改 `_check_task_access`（同 F4 / F23 cluster）。

**為什麼擴入而非留 follow-up**：

- 兩層 ACL 設計**必須對齊**，否則 #51 fix 後攻擊面只縮窄一半（ir.rule 層守住，controller 層仍 leaky）。
- F4 / F23 已躺 backlog 數個 cycle 沒人接 — 本 PR 順手收乾（合 #51 已是「擴 scope」，再擴 controller 一致性是邊際 cost）。

**為什麼不一併修 `_list_projects`（F10）**：

- F10 是 `with sudo()` + 空 domain 跨 tenant 列 — 與本 PR 改的 `_check_project_access` 同 controller，但**獨立 attack surface**（前者是 list, 後者是 read by ID）。修一條不修另一條沒意義；修兩條等於本 PR 變 「ai_assistant.py controller 整體 IDOR cleanup」— 超出合 #51 邊界。
- F10 留下一個 PR 與 F4 / F23 一起整理（已在 [[follow-ups]] reference 過）。

**Final scope（OQ-7 + OQ-8 wave 2 拍板後）**：本 design 落 (c) message_partner_ids walk + 擴入 `_check_project_access` 一個 method 的 unbound carve-out。**`_check_task_access` / `_list_projects` 留 follow-up 不擴**（F4 task path + F10 list path 獨立 attack surface）。

## Caveats（implementation 階段判斷）

### Caveat 2：`auto_created` rename 為 `sidecar_backed` 是否破壞既有 query

**檢查清單**：

```bash
grep -rn "auto_created" src/ extra/
```

預期 hit：
- `src/models/cloud_service.py:_model_auto_create_mcp_server` 內 `auto_created=True` write — 整個 method 刪了
- `src/controllers/paas.py:_auto_create_mcp_server` 內 `auto_created=True` write + search — 整個 method 刪了
- `src/models/mcp_server.py:93` field definition
- `src/tests/test_mcp_*.py` 可能引用

Implementation 階段做 grep + rename，本 design 階段不展開。

**替代**：保留 `auto_created` 字面，語意改成「URL 預填來自系統 / 由 user 自己填」— 不 rename。tradeoff：tag 名語意失準（auto_created 字面是「自動建」，但 B 案下沒有「自動建」path 了，user 還是要按 button 才有 record），對 maintainer 困惑。

**建議**：implementation 階段 rename 為 `sidecar_backed`。本 design 採此偏好但留 implementation 階段一個 callable judgement window。

### Caveat 3：URL allowlist 是否本 PR 加

**B 案下 user 可填 external URL**：
- Acceptable schemes: `https://`（推薦） / `http://`（local dev 用）
- Forbidden hosts: `169.254.169.254`（雲端 metadata）/ `localhost` / `127.0.0.0/8` / `10.0.0.0/8` / `172.16.0.0/12` / `192.168.0.0/16`（除非 user explicit override，但 PaaS 內部本不該 allow user-side internal probe）
- Cluster.local：B 案 sidecar-backed URL 本來就是 cluster.local，所以**不能 blanket ban**

**為什麼複雜**：URL allowlist 邏輯本身已是新 security gate，且 cluster.local 是 sidecar-backed 必要 — 不能簡單 ban。

**建議**：本 PR scope 內**加 minimal SSRF check**（拒絕 `169.254.169.254` + IPv6 link-local），其餘 internal-network filter **作為 #52 frontend hardening / new follow-up 處理**。本 design 在 plan AC 列出 minimal SSRF check 為必做。

## Out of scope（明文不擴）

- Issue #50 URL pattern 統一（`/api/workspaces/<wid>/projects/<pid>/...` nesting）— 獨立 PR
- Issue #52 frontend UX hardening（R-3 / R-6 / E2E-G1）— 獨立 PR
- F1-F28 backlog 除 **F22 + F23** cluster 以外（特別是 F4 task-side / F10 `_list_projects` / F12 sanitize / F14 sudo mapped）
- `controllers/ai_assistant.py:_check_task_access`（F4 task path） — 留 follow-up，本 PR 只擴 `_check_project_access`（surface 段已敘事）
- `controllers/ai_assistant.py:_list_projects`（F10 sudo empty domain leak） — 留 follow-up，本 PR 不擴
- Mcp_server.url 改為 dynamic compute（PR #47 §Inv #7 已 caveat，本 PR 不動）
- system-scope MCP ACL 收緊（PR #47 Risk #3，仍 out of scope）

## Open questions（已 resolved）

| OQ | 內容 | 拍板結論 | 拍板時間 / by |
|---|---|---|---|
| **OQ-1** | A / B / C 拍板 | **B**（保留 sidecar + UI shortcut + 不自動建） | 2026-05-20 user |
| **OQ-2** | `auto_created` rename or 保留 | **rename `sidecar_backed`**（implementation 階段 grep 範圍若 > 5 處 cross-module 引用回退 deprecated） | 2026-05-20 leader judges as implementation detail（design 偏好已敘事，user 不重複問） |
| **OQ-3** | Caveat 1 ACL domain 處理 | **(a) 合 #51 一輪** — 本 PR 含 F22 + F23 cluster；6-rule split workspace_user 對等改 dual-leg；OR-7 sub-decision 採 (c) message_partner_ids walk | 2026-05-20 user (composite); OR-7 sub-pick by planner recommendation per leader sign-off |
| **OQ-4** | Caveat 3 URL allowlist 範圍 | **minimal SSRF check（cloud metadata IPv4 + IPv6 link-local + scheme allowlist）**；完整 internal-network filter 留 #52 | 2026-05-20 leader judges as implementation detail（design 偏好已敘事，user 不重複問） |
| **OQ-5** | Migration 既有 `auto_created=True` records | **(c) 保留 + rename flag**（既有 row backfill `sidecar_backed=True`） | 2026-05-20 user |
| **OQ-6** | UI 拆兩按鈕 vs 統一 toggle | **拆兩按鈕**（Add Sidecar MCP / Add External MCP 並列；mental model 清楚） | 2026-05-20 user |

## References

- Issue #49：https://git-prod.woowtech.io/odoo-addons/woow_paas_platform/issues/49
- Predecessor decision：`docs/decisions/refactor/mcp-project-decouple.md`（PR #47 8 invariants）
- Predecessor patterns：
  - `docs/decisions/patterns/cloud-service-unlink-override-cleanup.md`
  - `docs/decisions/patterns/mcp-acl-6-rule-split.md`
- Predecessor plan：`docs/plans/2026-Q2/2026-05-18-mcp-project-decouple-plan.md`
- Predecessor spec（OR-4 / OR-5 來源）：`docs/plans/2026-Q2/2026-05-19-project-detail-page-design.md`
- Follow-up cluster：`docs/tracking/followups/follow-ups.md` F22
- Code paths（design / plan affected）：
  - `src/models/mcp_server.py:127-139` D5 constraint
  - `src/models/mcp_server.py:93-98` `auto_created` field
  - `src/models/cloud_service.py:849 / 914 / 940` 3 call sites
  - `src/models/cloud_service.py:970-1013` `_model_auto_create_mcp_server`
  - `src/models/cloud_service.py:1015-1036` `_model_build_mcp_endpoint_url`
  - `src/models/cloud_service.py:1119-1174` `_do_cleanup`
  - `src/models/cloud_service.py:1176-1201` `unlink()` override
  - `src/models/cloud_service.py:478, 671-735` `_build_mcp_sidecar_config` + `patch_sidecar` call
  - `src/models/cloud_app_template.py:120-156` 7 mcp_* fields
  - `src/controllers/paas.py:2395 / 2493 / 2560` 3 call sites
  - `src/controllers/paas.py:2601-2660` `_auto_create_mcp_server`
  - `src/controllers/paas.py:2662-2696` `_build_mcp_endpoint_url`
  - `src/controllers/paas.py:1504-1556` `_update_support_project`（OR-4 manual D5 query）
  - `extra/paas-operator/src/api/releases.py:471-587` `patch_sidecar` endpoint
  - `extra/paas-operator/src/api/releases.py:611-705` `_create_sidecar_service_and_route`
  - `extra/paas-operator/src/api/releases.py:231-244` release teardown sidecar cleanup
  - `src/data/cloud_app_templates.xml:89-100` n8n template demo data

## Sign-off log

| Wave | Date | OQ | 拍板者 | 拍板結論 |
|---|---|---|---|---|
| 1 | 2026-05-20 | OQ-1（URL 三方案 A/B/C） | user | **B**（保留 sidecar + UI shortcut + 不自動建 MCP record） |
| 1 | 2026-05-20 | OQ-3（Caveat 1 ACL domain 處理） | user | **(a) 合 #51 一輪**（本 PR + #51 共 PR / 共 review） |
| 1 | 2026-05-20 | OQ-5（既有 auto_created records migration） | user | **(c) 保留 + rename flag** 為 `sidecar_backed` |
| 1 | 2026-05-20 | OQ-6（UI 拆兩按鈕 vs 統一 toggle） | user | **拆兩按鈕**（Add Sidecar MCP / Add External MCP 並列） |
| 1 | 2026-05-20 | OQ-2（auto_created rename 細節） | leader | implementation 階段判斷（design 偏好 rename，grep > 5 cross-module 回退 deprecated） |
| 1 | 2026-05-20 | OQ-4（URL allowlist 範圍） | leader | implementation 階段判斷（design 偏好 minimal SSRF check：cloud metadata IP + IPv6 link-local + scheme allowlist） |
| 2 | 2026-05-20 | OQ-7（ai_assistant.py `_check_project_access` 擴入） | user | **YES**（minimal 擴入 project path 一個 method；task / list path 留 follow-up） |
| 2 | 2026-05-20 | OQ-8（OR-7 unbound ACL leg 2 三 options） | user | **(c) message_partner_ids walk**（與 [[cloud-service-project-decouple]] D3=A 既有 idiom 對齊） |

**Design + plan 雙 wave 全 sign-off。** Implementation scope（B 案 + 合 #51 + ai_assistant 擴入）已對齊；tester 可開動全 AC。
