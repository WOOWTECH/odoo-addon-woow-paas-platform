---
name: test-coverage-agent-runtime-phase-c-2026-05-24
phase: red-explained
team: feature-agent
test_explainer: agent-runtime-pod-test-explainer
plan_ref: docs/plans/2026-Q2/agent-runtime-pod-plan.md
design_ref: docs/plans/2026-Q2/agent-runtime-pod-design.md
tester_commit: fb3dc9d
created: 2026-05-24
updated: 2026-05-24
status: ready-for-implementation
related:
  - docs/tracking/test-reports/test-coverage-agent-runtime-phase-a-2026-05-22.md
  - docs/decisions/patterns/constraint-input-trust-expansion.md
  - docs/decisions/patterns/attachment-row-level-acl-and-anchor-stamping.md
  - docs/tracking/test-reports/test-coverage-mcp-refactor-2026-05-20.md
---

# Test Coverage Report — Agent Runtime Pod Phase C（人類可讀版）

> 這份報告**不是** plan AC 的逐條翻譯（plan 是 source-of-truth）。
> 它解釋：**「為何 Phase C 這樣測、為何分兩個 test 體系、哪些今天綠是 by design」**，餵給 reviewer / CAQA / stakeholder 對齊測試意圖。
>
> **reviewer special attention**：
> 1. 第 2 章 **雙 test 體系**（pytest paas-operator vs Odoo addon）— 為何分、各測哪一半、contract 在哪對接
> 2. 第 4 章 **frozen contract**（skill-bundle endpoint ⇄ Phase B `cache.py`）— cross-repo 無法 import 對方，靠凍結契約對齊
> 3. 第 5 章 **HC-2 第三度應用**（token authoritative，SB.4/SB.4b）— `[[constraint-input-trust-expansion]]` 在本 epic 第三次落地
> 4. 第 6 章 **today-green 分兩類**：6 條 regression-lock（明確 STAY GREEN）+ 4 條 negative-path **vacuous-today**（endpoint 不存在的 404 fallback 剛好 match negative 期望，不是漏寫的 red、也不是「實作已對」）
> 5. 第 8 章 **2 OQ**：secret 來源（planner clarify 中）+ migration 27 形式

---

## 1. TL;DR

Phase C 是 wiring epic 的關鍵接點 — 把 **Phase A schema**（agent_session / skill / project_access models）與 **Phase B image**（agent-runtime runtime + `cache.py` skill fetch）端到端接起來，但 **SSE 還沒真的走 pod**（`use_agent_runtime` flag default False，dual-mode 只加 gate 不開）。

Phase C 開通 4 條接線：

| 接線 | 端 | 對應 design |
|---|---|---|
| paas-operator `/api/agent-runtimes/*` endpoints + jwt_signer + webhook_dispatcher + mcp_invalidator | **paas-operator**（FastAPI） | §2.11 / §2.11.1 / §5.3 / §5.5 / §6.6 |
| skill-bundle download endpoint（frozen contract 對 Phase B cache.py）+ agent_session sync 欄位 | **Odoo addon** | §3.3 / §5.3 / §2.12 |
| agent_runtime_client + webhook receiver + drift cron | **Odoo addon** | §5.5 / §5.6 / §2.11 |
| SSE generator dual-mode flag gate（default False） | **Odoo addon** | §5.6 / §1.2 |

對應 test：**4 個 test 檔、52 個 test method**（pytest 24 + Odoo 28 = 12+11+5），跨 **pytest + Odoo 雙體系**。tester（commit `fb3dc9d`）報 red phase **~8 PASS / ~42 FAIL/ERROR**：

| 狀態 | 約數 | 含義 |
|---|---:|---|
| **today-green regression-lock** | 6 | R.1×2（既有 router 不動）+ SSE.1/2/3/5（flag-default + frontend 契約 + csrf 順序 + legacy path）— **明確 STAY GREEN** |
| **today-green negative-path vacuous** | 4 | SB.4b / SB.5 / SB.6 / WH.4 — endpoint 今天不存在 → Odoo 回 404，剛好 match 這些 negative 期望（**vacuous，非實作已對**） |
| **FAIL（真未實作）** | ~42 | jwt_signer / endpoints / skill-bundle 200-path / client / webhook receiver 200-path / drift cron / SSE.4 dual-mode branch — 全部 plan 預期 red |

> **注**：method 實際數 52（brief 報 54 — 差在 sse_gateway 實際 5 method 非 7）。today-green 嚴格靜態分類為 6 regression + 4 vacuous = 10；tester 報「8 PASS」係 runtime 統計（HttpCase 對不存在 route 的 fallback 行為令部分 vacuous-candidate 落入 FAIL/ERROR）。本報告以 **docstring red-expectation + 靜態 status 推斷** 分類，不逐條湊統計數字 — 見第 6 章 narrative。

---

## 2. 雙 test 體系 — 為何分、各測什麼、contract 在哪對接

Phase C 是**跨 process / 跨 repo** 的 wiring，所以 test 天然分兩個體系。reviewer 看 diff 時要知道「這條 test 在哪個 runner 跑、mock 了什麼」。

### 2.1 體系對比

| 維度 | ① pytest（paas-operator） | ②③④ Odoo（addon） |
|---|---|---|
| 檔案 | `extra/paas-operator/tests/test_agent_runtimes.py` | `src/tests/test_agent_pod_phase_c_*.py` |
| Runner | `cd extra/paas-operator && pytest tests/ -v` | `odoo-bin --test-enable --test-tags woow_paas_platform` |
| 測試基底 | FastAPI `TestClient(app)` | `TransactionCase` / `HttpCase` |
| 依賴 | 純 Python 3.11 venv；**mock helm + k8s 全部** | Odoo registry + **Phase A models 已 merge** |
| 物理現實 | paas-operator 是獨立 FastAPI service（包 helm CLI），有自己的 pytest + `-m integration` 真 k8s suite（見 CLAUDE.md） | Odoo 18 module |
| Red 機制 | **in-function import**（`from src.services import jwt_signer` 在 test body 內）→ 缺 module 時是 *clean per-test FAIL*，不汙染 collection | `setUpClass` guard（model 缺 → RuntimeError）+ HttpCase 對未實作 route 回 404 |

### 2.2 各測哪一半（contract 對接點）

```
   ┌─────────────────── paas-operator (pytest ①) ───────────────────┐
   │  jwt_signer.sign_ws_token / sign_skill_bundle_token / verify    │
   │  endpoints: spawn-or-attach / reap / resume / sync / status     │
   │  webhook_dispatcher (HMAC sign + retry)                         │
   │  mcp_invalidator (ConfigMap patch)                              │
   └──────────────┬───────────────────────────────┬─────────────────┘
                  │ JWT (HS256 shared secret)      │ HMAC-SHA256 webhook
                  │ frozen: token claims           │ X-PaaS-Signature
                  ▼                                 ▼
   ┌─────────────────────── Odoo addon (②③④) ──────────────────────┐
   │  ② skill-bundle endpoint  ← Bearer SKILL_BUNDLE_TOKEN (verify)  │
   │     agent_session sync fields + migration 27                    │
   │  ③ agent_runtime_client (→ POST operator) + webhook receiver    │
   │     (← HMAC verify) + drift cron (→ get_status fallback)        │
   │  ④ SSE generator dual-mode gate (use_agent_runtime flag)        │
   └─────────────────────────────────────────────────────────────────┘
                  ▲
                  │ frozen contract（第 4 章）
   ┌──────────────┴──────────────── Phase B repo ────────────────────┐
   │  paas-agent-runtime  runtime/skills/cache.py  _fetch_bundle      │
   │  (GET skill-bundle → resp.read() → sha256) — 本 repo 無法 import  │
   └──────────────────────────────────────────────────────────────────┘
```

**為何分兩體系是對的**：
- paas-operator 端的 signer / endpoint 邏輯**不依賴 Odoo registry** — 用 pytest mock k8s/helm 跑得快（CLAUDE.md：~10s）、隔離乾淨
- Odoo 端的 endpoint / client / cron **必須在 Odoo runtime**（ir.config_parameter / ir.cron / HttpCase route）才有意義
- 兩端在 **JWT（token claims）+ HMAC（webhook sig）+ frozen HTTP contract** 三處對接 — 各自測自己那半 + 把對接契約「凍結」成對方期望（見第 4 章）

**reviewer 提醒**：pytest 塊①的 red 是 `ModuleNotFoundError`（module 還沒建）；Odoo 塊②③④的 red 是 `404`（route 還沒建）或 field/cron 缺。**兩種 red 形態不同但都是「feature missing」乾淨訊號** — 不要把 pytest 的 import FAIL 當成 collection 壞掉。

---

## 3. Cell Map — Test ↔ Design §（52 method）

> **legend**：**explicit** = docstring 直接引用該 §；**+implicit** = 同時透過 walk/claim/contract 間接覆蓋另一 §；**regression-lock** = 今天綠、明確 STAY GREEN；**vacuous-today** = 今天綠但因 endpoint-404 fallback（見第 6 章）。

### 3.1 塊① pytest — `extra/paas-operator/tests/test_agent_runtimes.py`（24 method）

| Test method | AC | Design § (explicit) | +implicit-through | Type |
|---|---|---|---|---|
| `test_sign_ws_token_claims` | J.1 | §5.3（HS256 + per-invocation claims） | — | explicit |
| `test_sign_skill_bundle_token_claims` | J.2 | §5.3 + §3.3（project_id authoritative claim） | HC-2 [[constraint-input-trust-expansion]]（authoritative claim 是 SB.4 的 signer 端根基） | explicit + implicit |
| `test_token_scopes_are_distinguishable` | J.3 | §5.3（兩 token scope）+ §2.11（ws vs skill_bundle） | HC-2（ws_token 不帶 project_id → 不能被誤用為 bundle access；scope 分離是 HC-2 可行的前提） | explicit + implicit |
| `test_verify_roundtrip_current_secret` | J.4 | §5.3 | — | explicit |
| `test_verify_dual_secret_previous_fallback` | J.5 | §6.6（dual-secret rotation window） | — | explicit |
| `test_verify_rejects_unknown_secret` | J.6 | §6.6 | — | explicit |
| `test_verify_rejects_expired` | J.7 | §5.3（exp 1h）+ §6.6 | — | explicit |
| `test_verify_jti_replay_rejected` | J.8 | §6.6（jti replay defense） | — | explicit |
| `test_spawn_or_attach_cold_start_signs_both_tokens` | S.1 | §2.11（spawn-or-attach）+ §2.11.1（inject SKILL_BUNDLE_TOKEN at install） | — | explicit |
| `test_spawn_or_attach_attach_existing_is_noop` | S.2 | §2.11（attach no-op） | — | explicit |
| `test_spawn_or_attach_resume_resigns_token_before_scale` | S.3 | §2.11.1（resume re-sign + patch env BEFORE scale-1；escalation #1 sign-off 2026-05-23） | — | explicit |
| `test_reap_scales_to_zero` | S.4 | §2.11（reap scale-0） | — | explicit |
| `test_reap_already_reaped` | S.4 | §2.11（idempotent already_reaped） | — | explicit |
| `test_reap_not_found` | S.4 | §2.11（idempotent not_found） | — | explicit |
| `test_resume_resigns_and_patches_env_before_scale` | S.5 | §2.11.1（dedicated resume token lifecycle） | — | explicit |
| `test_invalidate_mcp_delegates` | S.6 | §2.11 + §5.4（mcp_invalidator delegate） | — | explicit |
| `test_sync_workspaces_helm_upgrade` | S.7 | §2.11（sync-workspaces helm upgrade） | §6.3（NetworkPolicy egress allowlist re-render — 跨 workspace project 的物理隔離前提） | explicit + implicit |
| `test_delete_uninstalls` | S.8 | §2.11（DELETE helm uninstall） | — | explicit |
| `test_status_returns_state` | S.9 | §2.11（GET status snapshot） | — | explicit |
| `test_dispatcher_signs_hmac_header` | W.1 | §5.5（webhook HMAC-SHA256 sign → X-PaaS-Signature） | — | explicit |
| `test_dispatcher_retries_on_5xx` | W.2 | §5.5（retry 3x exp backoff） | — | explicit |
| `test_invalidator_patches_configmap` | M.1 | §5.4（patch mcp.json ConfigMap） | — | explicit |
| `test_releases_invalid_namespace_still_400` | R.1 | §2.11.2（既有 releases router 不動） | — | **regression-lock** |
| `test_namespaces_invalid_name_still_400` | R.1 | §2.11.2（既有 namespaces router 不動） | — | **regression-lock** |

### 3.2 塊② Odoo — `test_agent_pod_phase_c_skill_bundle.py`（12 method）

| Test method | AC | Design § (explicit) | +implicit-through | Type |
|---|---|---|---|---|
| `test_AS1_last_synced_at_field` | AS.1 | §5.5（state sync 時戳 field） | — | explicit |
| `test_AS2_last_state_event_ts_field` | AS.2 | §5.5（drift detection 時戳） | — | explicit |
| `test_AS3_columns_additive_nullable` | AS.3 | §2.12 + §7.0.7（additive nullable idiom） | **OQ#2**（migration 27 形式：ORM auto-add vs 顯式 migrate） | explicit + implicit |
| `test_MIG1_manifest_version_bumped_to_27` | MIG.1 | plan §Phase C Task 13（manifest 26 → 27） | **OQ#2**（version bump 是 cron data + migration 27 載入前提） | explicit + implicit |
| `test_SB1_valid_token_returns_raw_bundle_bytes` | SB.1 | §3.3（ir.attachment storage）+ **frozen contract**（raw tar.gz bytes，非 base64/JSON） | — | explicit |
| `test_SB2_missing_token_unauthorized` | SB.2 | §3.3 + §5.3（Bearer token verify） | — | explicit |
| `test_SB3_expired_token_unauthorized` | SB.3 | §5.3（exp）+ §6.6 | — | explicit |
| `test_SB3b_bad_signature_unauthorized` | SB.3b | §5.3 + §6.6（HS256 verify current/previous） | — | explicit |
| `test_SB4_token_authoritative_serves_token_project` | SB.4 | §3.3 + **HC-2** [[constraint-input-trust-expansion]]（取 token project_id，不信 query param） | — | explicit（HC-2 第三度） |
| `test_SB4b_query_param_cannot_redirect_to_foreign_project` | SB.4b | §3.3 + **HC-2**（query project_hash 不能 redirect 到他人 project） | — | explicit（HC-2）+ **vacuous-today** |
| `test_SB5_skill_outside_token_project_denied` | SB.5 | §3.3 + **HC-2**（cross-project 無 bundle exfil） | — | explicit（HC-2）+ **vacuous-today** |
| `test_SB6_unpublished_skill_not_found` | SB.6 | §3.3（state='published' gate） | — | explicit + **vacuous-today** |

### 3.3 塊③ Odoo — `test_agent_pod_phase_c_client_webhook.py`（11 method）

| Test method | AC | Design § (explicit) | +implicit-through | Type |
|---|---|---|---|---|
| `test_CL1_factory_returns_configured_or_none` | CL.1 | §5.6（get_agent_runtime_client，仿 get_paas_operator_client） | — | explicit |
| `test_CL2_spawn_or_attach_posts_with_api_key` | CL.2 | §2.11 + §5.6（POST spawn-or-attach + X-API-Key） | — | explicit |
| `test_CL3_reap_posts` | CL.3 | §2.11（POST /{hash}/reap） | — | explicit |
| `test_CL4_sync_workspaces_posts_ids` | CL.4 | §2.11（POST sync-workspaces + project_workspace_ids） | §6.3（egress allowlist 同步觸發源） | explicit + implicit |
| `test_CL5_get_status_gets` | CL.5 | §2.11（GET /{hash}/status） | — | explicit |
| `test_WH1_valid_hmac_updates_session` | WH.1 | §5.5（valid HMAC → state + last_synced_at + last_state_event_ts 更新） | — | explicit |
| `test_WH2_invalid_hmac_rejected` | WH.2 | §5.5（invalid HMAC → 401，session 不動） | — | explicit |
| `test_WH3_missing_signature_rejected` | WH.3 | §5.5（missing sig → 401） | — | explicit |
| `test_WH4_unknown_hash_not_found` | WH.4 | §5.5（unknown project_hash → 404） | — | explicit + **vacuous-today** |
| `test_CR1_drift_cron_exists_and_active` | CR.1 | §5.5（5-min fallback poll cron） | — | explicit |
| `test_CR2_cron_calls_get_status_for_stale_session` | CR.2 | §5.5（stale session → get_status re-sync） | — | explicit |

### 3.4 塊④ Odoo — `test_agent_pod_phase_c_sse_gateway.py`（5 method）

| Test method | AC | Design § (explicit) | Type |
|---|---|---|---|
| `test_SSE1_use_agent_runtime_defaults_false` | SSE.1 | §5.6 + Phase A A.8（flag dormant default False） | **regression-lock** |
| `test_SSE2_frontend_eventsource_url_unchanged` | SSE.2 | §1.2 + §5.6（frontend `/api/ai/stream/` + csrf_token 零變更） | **regression-lock** |
| `test_SSE3_csrf_gate_first_regardless_of_flag` | SSE.3 | §5.6 步驟①（csrf gate 必須最先，security ordering） | **regression-lock** |
| `test_SSE4_flag_true_takes_runtime_branch` | SSE.4 | §5.6 步驟②③④（flag True → runtime branch，error_code ≠ legacy `no_agent`） | explicit（FAIL 真未實作） |
| `test_SSE5_flag_false_keeps_legacy_path` | SSE.5 | §5.6（flag False → legacy assistant path → `no_agent`） | **regression-lock** |

### 3.5 Cell-map coverage 統計

| Type | Count |
|---|---:|
| **Pure explicit** | 35 |
| **Explicit + implicit-through** | 7（HC-2: J.2/J.3 · §6.3: S.7/CL.4 · OQ#2: AS.3/MIG.1 · SB.4） |
| **Regression-lock**（today-green，明確 STAY GREEN） | 6（R.1×2 + SSE.1/2/3/5） |
| **Negative-path vacuous-today** | 4（SB.4b/SB.5/SB.6/WH.4；SB.4b/SB.5 同時帶 HC-2 implicit） |
| **Total** | **52** |

**implicit-through 集中在哪**：
- **HC-2 [[constraint-input-trust-expansion]]**（J.2 / J.3 signer 端 + SB.4 / SB.4b / SB.5 endpoint 端）— token authoritative 是 signer 與 endpoint 兩端協同
- **§6.3 NetworkPolicy egress**（S.7 / CL.4）— sync-workspaces 是跨 workspace project 物理隔離的觸發源
- **OQ#2 migration 27 形式**（AS.3 / MIG.1）— additive nullable column 與 cron data 載入機制

---

## 4. Frozen Contract — skill-bundle endpoint ⇄ Phase B `cache.py`

> **reviewer special attention**：這是 Phase C 唯一的 **cross-repo 契約鎖點**。

### 4.1 為何要「凍結」契約

skill-bundle endpoint（Odoo 本 repo）的消費者是 **Phase B `paas-agent-runtime` image 的 `runtime/skills/cache.py`**（另一個 repo）。兩 repo 無法 import 對方測試 — 所以 tester 把 `cache.py` 的 `_fetch_bundle` 行為**凍結**成 SB.* 的期望（leader 已 cross-check `cache.py` 原始碼，**zero guessing**）。

凍結的契約（design §3.3 fetch 流程 + test docstring）：

```
GET {ODOO_BASE_URL}/api/agent-runtimes/skill-bundle
      ?project_hash={hash}&skill_name={name}&version={ver}
header: Authorization: Bearer {SKILL_BUNDLE_TOKEN}
→ 200 returns the *raw* tar.gz bytes  (cache.py does resp.read() then hashlib.sha256(data))
→ any 4xx/5xx raises SkillFetchError on the pod side
```

### 4.2 SB.1 為何三重 assert raw bytes

`test_SB1_valid_token_returns_raw_bundle_bytes` 連續三條 assert：

1. `resp.status_code == 200` — 正常路徑
2. `resp.content == self.bundle_bytes` — body **必須是 raw bytes**
3. `resp.content != base64.b64encode(self.bundle_bytes)` — **防呆**：body 不可是 base64

**為何防 base64**：Odoo `ir.attachment.datas` 內部存 base64（fixture 用 `base64.b64encode(bundle_bytes)` 寫入）。implementer 最自然的錯誤是直接 return `attachment.datas`（= base64 string）。但 `cache.py` 拿到後直接 `hashlib.sha256(data)` 算 hash — 若 body 是 base64，hash 對不上 `bundle_sha256` → pod 端 SkillFetchError。**這條 assert 鎖死 implementer 必須 `base64.b64decode(attachment.datas)` 後 return raw bytes**。

**reviewer 對照點**：看 endpoint return 必須是 `Response(base64.b64decode(attachment.datas), content_type='application/gzip')`，**不是** `attachment.datas`、**不是** JSON wrapper。改錯這條 = SB.1 紅 + Phase B 端整條 skill fetch 在 sha256 verify 斷。

---

## 5. HC-2 第三度應用 — Token Authoritative（SB.4 / SB.4b）

> **HC-2 = `[[constraint-input-trust-expansion]]`**：attacker-controllable input 不可用來推導 authority / 解析 resource ownership。

### 5.1 本 epic 第三度落地

| 度 | 場景 | 落地 |
|---|---|---|
| 第一度 | m2m write constraint grantor authority（pattern doc 原案） | refactor branch |
| 第二度 | §5.2 `user_message` envelope：`channel_id` / `invoker_role` payload 同名欄 **NOT security decision**，security 一律取 token claims（design 明寫 HC-2 image 端 instance） | Phase B image |
| **第三度** | **skill-bundle endpoint：project resolution 取 token 的 `project_id` claim，不信 query param `project_hash`** | **Phase C 本次** |

### 5.2 SB.4 / SB.4b 怎麼鎖

fixture 刻意設兩個 project（A 有 published skill、B 無）+ 一個 `wrong_hash`（捏造的 query project_hash）：

- **SB.4**（正向）：token 帶 `project_id=A`，query `project_hash` 故意填 `'pa-some-other-project-hash'`（亂填）→ **仍須 200 + 回 A 的 bundle**。證明 query param 被忽略，token 說了算。
- **SB.4b**（反向 exfil 防線）：token 帶 `project_id=B`（B 無此 skill），query 填指向 A 的 hash → **必須非 200**。證明就算 query param 指 A，B 的 token 也拿不到 A 的 skill。

### 5.3 signer 端配套（J.2 / J.3）— 為何 HC-2 兩端都要鎖

HC-2 在 endpoint 端能成立，**前提是 signer 端讓 token scope 可信且不可混用**：

- **J.2**：`skill_bundle_token` **必須**帶 `project_id`（authoritative claim）— 否則 endpoint 無 authoritative 來源可取
- **J.3**：`ws_token` **必須不**帶 `project_id`，`skill_bundle_token` **必須不**帶 `channel_id` — 兩 scope disjoint。若 ws_token 也帶 project_id，攻擊者可拿 per-invocation 的 ws_token 去領 bundle（scope 越界）

**reviewer 對照點**：
- endpoint 端（SB.4/SB.4b）：看 controller 解析 project **必須** `claims['project_id']`，**禁止** `kwargs.get('project_hash')` 參與 ownership 判斷
- signer 端（J.2/J.3）：看 `sign_ws_token` 的 payload **不含** project_id；`sign_skill_bundle_token` 的 payload **不含** channel_id
- 任一端破 = HC-2 第三度漏 → cross-project skill bundle exfil

---

## 6. Today-Green 分兩類 — reviewer 不要誤判

Phase C red phase 有約 10 條今天就綠（嚴格靜態），分兩種性質完全不同的類別。**混淆會導致 reviewer 誤判實作狀態**。

### 6.1 Regression-lock（6 條）— 明確 STAY GREEN

docstring 明寫「PASS today and MUST STAY GREEN post-impl」：

| Test | 鎖什麼 | impl 後若變紅代表 |
|---|---|---|
| R.1 `test_releases_invalid_namespace_still_400` | 既有 releases router 不受新 router 干擾（§2.11.2） | 新 router 改壞了既有 router 的 validation |
| R.1 `test_namespaces_invalid_name_still_400` | 既有 namespaces router 不動 | 同上 |
| SSE.1 `..._defaults_false` | `use_agent_runtime` flag dormant（Phase A A.8 延續） | 有人把 default 改 True → 全 prod chat 被切到未完成的 pod path |
| SSE.2 `..._eventsource_url_unchanged` | frontend `ai_service.js` 的 `/api/ai/stream/` + csrf_token 零變更（§1.2 / §5.6 frontend zero-change 契約） | backend rewrite 移了 SSE 入口 → frontend 斷 |
| SSE.3 `..._csrf_gate_first_...` | csrf gate 必須最先（security ordering，無論 flag） | rewrite 把 csrf 檢查移到 flag/project 解析之後 → 安全漏洞 |
| SSE.5 `..._flag_false_keeps_legacy_path` | flag False → 既有 assistant path → `no_agent`（production 行為不變） | dual-mode 改動污染了 legacy path |

### 6.2 Negative-path vacuous-today（4 條）— 今天綠是「巧合」

這 4 條今天綠，**不是因為實作對了，是因為 endpoint 還不存在 → Odoo 回 404，剛好 match negative 期望**：

| Test | 期望 | 今天綠的原因 | impl 後的真義 |
|---|---|---|---|
| SB.6 `..._unpublished_skill_not_found` | `status == 404` | endpoint 不存在 → 404（巧合 match） | endpoint 存在後，draft skill 仍須 404（state gate） |
| WH.4 `..._unknown_hash_not_found` | `status == 404` | endpoint 不存在 → 404（巧合 match） | endpoint 存在後，unknown hash 仍須 404（非 500/200） |
| SB.5 `..._skill_outside_token_project_denied` | `status ∈ (403,404)` | endpoint 不存在 → 404（巧合 match） | endpoint 存在後，cross-project skill 須 403/404（HC-2） |
| SB.4b `..._query_param_cannot_redirect...` | `status != 200` | endpoint 不存在 → 404 ≠ 200（巧合 match） | endpoint 存在後，B token 領 A skill 須非 200（HC-2 exfil 防線） |

**為何這個區分重要**（test-explainer 的核心價值）：

1. **reviewer 不能因「這 4 條今天綠」就以為 negative-path 已守** — 它們今天 vacuous，impl 後才從「巧合綠」變「meaningful 綠」
2. **危險陷阱**：implementer 寫了 endpoint 但忘了 negative gate（例如 SB.6 沒檢查 `state=='published'`），這 4 條會**先變紅再變綠**（endpoint 存在但 gate 漏 → 回 200 → 紅 → 補 gate → 綠）。若 reviewer 以為「本來就綠」，會錯過這個 transition
3. **正確心智模型**：impl 過程中，這 4 條的綠燈會「破再立」。**reviewer 應主動驗證 impl 後它們仍綠、且是因為 gate 邏輯（非 fallthrough 404）**

> 對照：Phase A report §5 的 3 條 regression-lock（C.4/R.3/R.9）是「今天綠且 impl 全程不該破」；Phase C 的 vacuous 4 條是「今天綠但 impl 中會破再立」— **兩者都是 today-green，但 reviewer 應對方式相反**。

---

## 7. Fail Reason 對齊 Design — 為何 fail = design 故意要求的 new behavior

### 7.1 塊① pytest（22 FAIL，R.1×2 PASS）

| 群 | Fail reason | Design 故意要求 |
|---|---|---|
| J.1-J.8 | `from src.services import jwt_signer` → ModuleNotFoundError | §5.3 + §6.6 — jwt_signer module 未建 |
| S.1-S.9 | `patch("src.api.agent_runtimes.<svc>")` → router module 不存在 | §2.11 + §2.11.1 — agent_runtimes router 未建 |
| W.1-W.2 | `from src.services import webhook_dispatcher` → ModuleNotFound | §5.5 — webhook_dispatcher 未建 |
| M.1 | `from src.services import mcp_invalidator` → ModuleNotFound | §5.4 — mcp_invalidator 未建 |

### 7.2 塊② skill_bundle（8 FAIL，4 vacuous green）

| Test | Fail reason | Design 故意要求 |
|---|---|---|
| AS.1 / AS.2 | field 不在 `agent_session._fields` | §5.5 — sync 時戳欄位 |
| AS.3 | DB column 不存在（information_schema 空） | §2.12 + §7.0.7 — additive nullable column（OQ#2） |
| MIG.1 | manifest 仍 18.0.1.0.26 | plan Task 13 — bump 27（OQ#2） |
| SB.1 | endpoint 不存在 → 404（期望 200 raw bytes） | §3.3 + frozen contract |
| SB.2 / SB.3 / SB.3b | endpoint 不存在 → 404（期望 401） | §5.3 + §6.6 — token verify |
| SB.4 | endpoint 不存在 → 404（期望 200，token-authoritative 正向） | §3.3 + HC-2 |

### 7.3 塊③ client_webhook（9 FAIL，WH.4 vacuous green）

| Test | Fail reason | Design 故意要求 |
|---|---|---|
| CL.1-CL.5 | `import services.agent_runtime_client` → ModuleNotFound（轉 self.fail） | §5.6 + §2.11 — client wrapper 未建 |
| WH.1 | endpoint 不存在 → 404（期望 200 + session 更新） | §5.5 — webhook receiver |
| WH.2 / WH.3 | endpoint 不存在 → 404（期望 401） | §5.5 — HMAC verify |
| CR.1 | 無 drift ir.cron（search 空） | §5.5 — fallback poll cron |
| CR.2 | cron 不存在 → 無法 trigger | §5.5 — stale session re-sync |

### 7.4 塊④ sse_gateway（1 FAIL，4 regression green）

| Test | Fail reason | Design 故意要求 |
|---|---|---|
| SSE.4 | 無 `use_agent_runtime` branch → flag-True 仍走 legacy → `no_agent`（期望 ≠ no_agent） | §5.6 步驟②③④ — dual-mode dispatch branch |

---

## 8. Open Questions（2 條）— impl 啟動前待 ruling

> tester 在 docstring 顯式標出兩個 OQ，**不阻塞 red phase**，但 impl 啟動前需 leader / planner ruling。

### OQ#1 — 共享 secret 來源（planner clarify 中）

**問題**：Odoo 端讀 HS256 / HMAC secret 的來源未定。

| 端 | test 假設（setUp） | design §5.3 假設 |
|---|---|---|
| skill-bundle token secret | `ir.config_parameter` key `woow_paas_platform.agent_runtime_auth_secret` | K8s Secret `agent-runtime-auth-secret`（mount） |
| webhook HMAC secret | `ir.config_parameter` key `woow_paas_platform.agent_runtime_webhook_secret` | K8s Secret `agent-runtime-webhook-secret`（mount） |

**影響範圍**：若 impl 改從 mounted K8s Secret file 讀，**只需調整 test setUp 的 secret-seed 那一行** — route contract + token-authoritative 行為 + HMAC verify 語意**全部不受影響**。test docstring 三處（skill_bundle setUp / client_webhook setUp / 本報告）都標了這個假設。

**reporter 提醒**：這是「實作細節 ruling」非「設計缺口」。test 用 config_parameter 是因為它對齊既有 `operator_url` / `operator_api_key` pattern（最低改動假設）；planner 若拍板走 K8s Secret mount，tester 補一行 setUp 即可，cell-map 不變。

### OQ#2 — migration 27 形式

**問題**：`agent_session.last_synced_at` / `last_state_event_ts` 兩個 additive nullable column 的落地形式未定。

- **選項 A**：純 ORM auto-add（Odoo `-u` 對 additive nullable column 自動建欄，無需手寫 migration）— AS.3 docstring 寫「migration 27 / ORM auto-add」即暗示這條最可能
- **選項 B**：顯式 `src/migrations/18.0.1.0.27/` pre/post-migrate（若需 backfill 或 cron data 載入順序保證）

**已確定的部分**：MIG.1 鎖死 manifest **必須** bump 到 18.0.1.0.27（否則 cron data + 任何 27 migration 都不載入）。AS.3 鎖死 column 必須 additive + **NULLABLE**（no NOT NULL、no backfill — 既有 row 存活）。

**未定**：column 的物理建立走 A 還是 B。**這不影響 AS.1/AS.2（field 存在）/ AS.3（column nullable）的 assertion** — 兩種形式都能讓 column 出現且 nullable。

**reporter 提醒**：AS.3 用 `information_schema` 查 column 是「形式無關」的驗證（ORM auto-add 或 migrate 建的 column 都查得到）。impl 選 A 或 B 都過 — 這是 tester 刻意留的 implementation latitude（對齊 Phase A migration test 對 Step 5 / Step 7 留 latitude 的做法）。

---

## 9. Scope 邊界 — Phase C 不做什麼（留 Phase D-G）

| 未測項目 | 留給 | 理由 |
|---|---|---|
| Pilot project chat 真走 pod path（end-to-end） | **Phase D** | `use_agent_runtime` flag default False；Phase C 只加 gate 不開（SSE.1 鎖 default False） |
| 真實 WS relay 15-type envelope（text_chunk / tool_call / done…） | **Phase D / paas-operator integration** | 需 live pod；SSE.4 只測 *dispatch decision*（哪個 branch），不測 relay |
| Pod-side token verify（dual-secret + jti 在 pod 端） | **paas-agent-runtime image**（`runtime/transport/auth.py`，非本 repo） | 塊①只測 operator-side signer + verify helper；pod 端 verify 是 Phase B image 責任 |
| SHA256 bundle integrity verify | **pod-side `cache.py`** | endpoint 只回 raw bytes；sha256 比對是 pod 責任（frozen contract 第 4 章） |
| HC-4 `/web/content` attachment side-channel | **Phase A `test_agent_pod_phase_a_attachment_acl.py`** | skill-bundle 是 *legitimate* bundle path；HC-4 守 *illegitimate* 那條（[[attachment-row-level-acl-and-anchor-stamping]]） |
| webhook *sender* retry/backoff | 塊① W.2（operator 端 `webhook_dispatcher`） | 塊③只測 *receiver*（Odoo 端 HMAC verify + state 更新） |
| 真 K8s pod spawn / attach | **paas-operator `-m integration` suite** | 塊①全 mock helm + k8s |
| cold-start latency / worker pool 觀察 | **Phase D pilot observation** | Phase C 無 runtime metric |

---

## 10. Fixture / 環境隱性假設（reviewer 必看）

1. **雙 runner**：塊① 跑 `cd extra/paas-operator && pytest`（FastAPI TestClient + API-key auth disabled via `patch("src.main.settings")`）；塊②③④ 跑 Odoo `--test-tags woow_paas_platform`（需 Phase A models merged）。**reviewer 別在同一個 runner 找全部 52 method**。

2. **pytest in-function import 是刻意的**：塊① 的 `from src.services import jwt_signer` 寫在 test body 內（非檔案頂部）。**目的**：缺 module 時是 *per-test clean FAIL*，其他已存在的 test（如 R.1 regression）仍正常 collect & run。implementer 建好 module 後不需動 test。

3. **stdlib HS256 簽章（無 PyJWT 依賴）**：塊②的 `_sign_hs256()` 用純 `hmac + base64` 自簽 token（PyJWT 在 Phase C 才隨 block ④ 落地）。**implementer 改 production 用 PyJWT 時，token wire format 必須與這個 stdlib 簽法相容**（標準 HS256 JWT — header `{"alg":"HS256","typ":"JWT"}` + payload + HMAC-SHA256 sig），否則 SB.* 的 token 驗不過。

4. **token-authoritative fixture 的 `wrong_hash`**：塊② setUp 造一個 `wrong_hash = 'pa' + uuid[:14]`，所有 SB.* 的 query `project_hash` 都故意填它（或更亂的字串）。**這是 HC-2 的可執行斷言** — query param 永遠是「錯的/亂的」，唯一正確來源是 token claim。implementer 看到 query param 被亂填不要「修正」它。

5. **webhook fixture 用真 HMAC 簽**：塊③ `_post_signed()` 用 `hmac.new(secret, body, sha256).hexdigest()` 真簽，header `X-PaaS-Signature`。**這個 header 名 + hexdigest 格式必須與塊①的 `webhook_dispatcher`（W.1）對齊** — 兩端都引用同一個契約（design §5.5）。

6. **agent_session 自動算 namespace_hash**：塊③ webhook fixture `create({'project_id':..., 'state':'spawning'})` 後直接讀 `session.project_namespace_hash`（Phase A N.6 的 compute）。**webhook receiver 必須 by `project_namespace_hash` 找 session**（design §5.5），不是 by project_id。

7. **SSE csrf token 抓取**：塊④ `_csrf_token()` 從 `/woow` 或 `/web` 渲染頁 regex 抓 `csrf_token = "..."`，docstring 明警告「capture WHOLE quoted value（`[0-9a-f]+` 會在 'o' 分隔符截斷 → malformed token）」。**這是 fixture 細節，csrf 抓失敗會 loud fail（非偽裝成 SSE red）**。

8. **SSE.4 是純 HTTP 行為探針**：刻意**不** import/mock 塊③ client module — 它的 red 精準指向「flag branch 缺失」，不是「未建依賴」。implementer 補 dual-mode branch 後，flag-True → runtime branch → 非 `no_agent` code 即綠。

---

## 11. 對 reviewer 的提醒（PR diff 對照導讀）

### 11.1 paas-operator（塊①）

- `extra/paas-operator/src/services/jwt_signer.py`（新）：`sign_ws_token`（無 project_id）/ `sign_skill_bundle_token`（有 project_id）/ `verify_token`（dual-secret + jti cache）→ 對 J.1-J.8（**J.2/J.3 是 HC-2 signer 端，看 claim 分離**）
- `extra/paas-operator/src/api/agent_runtimes.py`（新 router）：spawn-or-attach（**S.1 看 SKILL_BUNDLE_TOKEN 注入 helm install env**；**S.3 看 resume re-sign + patch env BEFORE scale**）/ reap / resume / invalidate-mcp / sync-workspaces / delete / status → 對 S.1-S.9
- `extra/paas-operator/src/services/webhook_dispatcher.py`（新）：HMAC-SHA256 + `X-PaaS-Signature` + retry 3x → 對 W.1/W.2
- `extra/paas-operator/src/services/mcp_invalidator.py`（新）：patch ConfigMap → 對 M.1
- `extra/paas-operator/src/main.py`：`include_router(agent_runtimes.router)` — **不准動既有 releases/namespaces router** → 對 R.1（regression-lock）

### 11.2 Odoo skill-bundle（塊②）

- `src/controllers/`（新 route `/api/agent-runtimes/skill-bundle`）：**回 `base64.b64decode(attachment.datas)` raw bytes**（非 datas / 非 JSON）→ 對 SB.1（frozen contract）；**project 解析取 `claims['project_id']` 不信 query param** → 對 SB.4/SB.4b/SB.5（HC-2）；token verify HS256 current/previous → 對 SB.2/SB.3/SB.3b；`state=='published'` gate → 對 SB.6
- `src/models/agent_session.py`：加 `last_synced_at` / `last_state_event_ts`（Datetime readonly nullable）→ 對 AS.1/AS.2/AS.3
- `src/__manifest__.py`：bump 18.0.1.0.27 → 對 MIG.1（**OQ#2：migration 27 形式待定**）

### 11.3 Odoo client + webhook + cron（塊③）

- `src/services/agent_runtime_client.py`（新）：`get_agent_runtime_client(env)`（仿 `get_paas_operator_client`，unconfigured → None）+ `spawn_or_attach`/`reap`/`sync_workspaces`/`get_status`（X-API-Key header）→ 對 CL.1-CL.5
- `src/controllers/`（新 route `/api/agent-runtimes/webhook/state-update`，auth=none type=json）：HMAC verify → by `project_namespace_hash` 找 session → update state + last_synced_at + last_state_event_ts → 對 WH.1；invalid/missing sig → 401 + 不動 session → 對 WH.2/WH.3；unknown hash → 404 → 對 WH.4
- `src/data/agent_session_cron.xml`（新）：drift detection cron（5-min，bound to agent_session，active）→ 對 CR.1；stale session（last_synced_at < now-10min）→ call `get_status` → 對 CR.2

### 11.4 Odoo SSE dual-mode（塊④）

- `src/controllers/ai_assistant.py`（rewrite SSE generator）：`if project.use_agent_runtime:` branch（§5.6 pseudocode）→ 對 SSE.4
  - **csrf gate 必須最先**（SSE.3 regression）
  - **flag False 維持 legacy `no_agent`**（SSE.5 regression）
  - **不動 frontend `ai_service.js`**（SSE.2 regression — frontend zero-change）
  - **不改 `use_agent_runtime` default**（SSE.1 regression — 維持 False）

---

## 12. Hand-off

**Audience**:
- **Implementer**：對照 §11 PR diff 導讀；特別注意（a）第 4 章 frozen contract — skill-bundle 回 raw bytes 非 base64；（b）第 5 章 HC-2 兩端 — endpoint 取 token project_id、signer 分離 token scope；（c）第 6 章 vacuous-today 4 條會「破再立」，補 endpoint 時別漏 negative gate。
- **Reviewer**：對照 §6 today-green 兩類（6 regression-lock 全程不破 vs 4 vacuous 破再立）；§5 HC-2 endpoint+signer 兩端；§4 frozen contract 的 base64 防呆。
- **CAQA**：兩端 contract lockstep verify — JWT claim 分離（§5）/ HMAC header 格式（§10.5）/ frozen HTTP contract（§4）；OQ#1 secret 來源 ruling 後補 invariant。
- **Stakeholder / onboarding**：讀 §1 TL;DR + §2 雙體系圖 + §9 scope 邊界 — 5 分鐘理解 Phase C 接了哪 4 條線、什麼留 Phase D-G。

**Reasoning gap 收尾**（Phase C red phase 後狀態）：

- ✅ 雙 test 體系（pytest vs Odoo）職責分離 + contract 對接點 → §2
- ✅ frozen contract（cache.py raw bytes）→ §4 + SB.1 三重 assert
- ✅ HC-2 第三度（token authoritative）endpoint + signer 兩端 → §5
- ✅ today-green 兩類區分（regression-lock vs vacuous）→ §6（避免 reviewer 誤判實作狀態）
- ⏭ **OQ#1 secret 來源**（config_parameter vs K8s Secret mount）→ planner clarify 中；不阻塞 red，impl 啟動前需 ruling（只影響 setUp 一行）
- ⏭ **OQ#2 migration 27 形式**（ORM auto-add vs 顯式 migrate）→ impl latitude；AS.3 用 information_schema 形式無關驗證，A/B 皆過

Phase C red phase 後**無 outstanding reasoning gap**。兩個 OQ 都是「impl 細節 ruling / latitude」非「設計缺口」，cell-map 不因 ruling 結果改變。

---

## See also

- [[test-coverage-agent-runtime-phase-a-2026-05-22]] — Phase A coverage report（同 epic 前一 phase；cell-map / today-green 區分 idiom 來源）
- [[agent-runtime-pod-design]] — design doc（§2.11 endpoints / §2.11.1 token lifecycle / §3.3 storage / §5.3 auth / §5.5 state sync / §5.6 SSE rewrite）
- [[agent-runtime-pod-plan]] — Phase C task breakdown
- [[constraint-input-trust-expansion]] — HC-2 pattern canonical home（token authoritative 第三度應用）
- [[attachment-row-level-acl-and-anchor-stamping]] — HC-4 attachment 防線（skill-bundle legitimate path 的對面）
- [[test-coverage-mcp-refactor-2026-05-20]] — regression-lock / vacuous-green narrative pattern reference
