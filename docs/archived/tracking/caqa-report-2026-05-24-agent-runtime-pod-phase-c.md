---
name: caqa-report-2026-05-24-agent-runtime-pod-phase-c
phase: caqa
team: agent-runtime-pod
caqa: agent-runtime-pod-caqa
plan_ref: docs/plans/2026-Q2/agent-runtime-pod-plan.md
design_ref: docs/plans/2026-Q2/agent-runtime-pod-design.md
test_report_ref: docs/archived/tracking/test-coverage-agent-runtime-phase-c-2026-05-24.md
created: 2026-05-24
related:
  - docs/archived/tracking/caqa-report-2026-05-22-agent-runtime-pod-phase-a.md
  - docs/decisions/patterns/constraint-input-trust-expansion.md
  - docs/decisions/patterns/attachment-row-level-acl-and-anchor-stamping.md
  - docs/tracking/pitfalls/cross-workspace-task-data-leak.md
  - docs/tracking/pitfalls/plan-vs-odoo-schema-mismatch.md
status: archived
archived-from: docs/tracking/caqa-report-2026-05-24-agent-runtime-pod-phase-c.md
archived: 2026-07-19
archived-reason: "一次性 CAQA 驗收報告，cycle 已收官；patterns/pitfalls 證據錨點引用已改指本處"
---

# CAQA Report — Agent Runtime Pod Phase C（epic wiring 收口：operator endpoints + skill-bundle + SSE bridge）

## Verdict

**PASS WITH CAVEATS** — 0 我方判定的 blocker，8 caveat（含 1 **disputed severity** 上呈 leader 裁決），6 info。

Phase C 是把 Phase A schema 與 Phase B image 端到端接起來的 wiring，**整段躲在 `project.use_agent_runtime` flag（default False）之後** → Phase C 出貨即 inert，不啟動 live pod 流量（`project_project.py:65` + gate `ai_assistant.py:395`，SSE.1 test 鎖定）。最高價值的 cross-repo frozen contract（skill-bundle endpoint ⇄ Phase B `cache.py`）**逐欄位驗證為精確相符，無漂移**。三條既有安全防線（HC-2 token authoritative、HC-4 attachment row-level、B1 project_access gate）**全部守住**。

四面向摘要：
- **Quality**：52 test method（pytest 24 + Odoo 28）dual-system + frozen-contract locked，覆蓋紮實；migration .27 additive/nullable/`ADD COLUMN IF NOT EXISTS` 可重放；少數 code idiom info（`datetime.utcnow()` deprecated、HS256 verify 雙實作、`new_state` 無 Selection 預檢）。
- **Security**（攻擊面最大）：**無新 B1-class data leak**；HC-2 第三度落地正確（skill-bundle 用 token claim project_id，不信 query param）；HC-4 未被 `sudo()` 繞過（machine-auth 獨立路徑）；resume token re-sign 順序正確（env patch 在 scale-1 之前，無 race）；dual-secret 不給 expired token 第二次機會。**caveat 群**：scope claim 缺（disputed）、operator auth fail-open、webhook 單 secret（偏離 design）、webhook 無 replay 防護、namespace secret 硬編碼 fallback + 缺 auto-gen（plan gap）、project_hash operator 邊界未驗格式、exc 字串洩漏到 SSE client。
- **Performance**：SSE worker-pin（每 active conversation 佔 1 Odoo worker — design §6.8 已認列 tradeoff，pool size 66）；async-gen 未 `aclose()`（client 斷線後 pod WS 殘留至 ping_timeout）；cron drift 5min sequential `get_status`（30s timeout each，stale 多時可能久）。
- **Architecture**：operator 既有 router 完全不動、helm.py 2 method 純 additive、WS flag default False（Phase C inert）、cross-repo contract 精確相符 — 全部確認 ✓；唯一 caveat 為 `websockets.connect(extra_headers=)` 在 websockets ≥14 已移除（latent break，plan-level 缺陷，Phase D 啟用前必修）。

> **Verdict 取決於 leader 對 disputed item C-1（scope claim）的裁決**：我方嚴格分析判 caveat（理由見 §2.1 + §7）；security-auditor sub-agent 判 blocker。若 leader 採 blocker → verdict 轉 **BLOCKED**；若採我方 caveat → 維持 **PASS WITH CAVEATS**。我**不靜默降級**，把爭議全揭露於下。

---

## 1. Diff scope 與 base 校正

| 項目 | 值 |
|------|----|
| Phase A merge base（乾淨 diff 起點） | `9479cb2`（Merge PR #58）— `git merge-base HEAD origin/develop` 確認 |
| 本地 `develop` ref | 落後（behind 20）→ **不可用 `develop..HEAD`**（會把 Phase A 檔帶進來，同 Phase A 那次踩坑） |
| Phase C 真實 diff | `9479cb2..HEAD` = 27 檔 / +3612 |
| 3 個 impl commit | `6abb490`（operator）/ `ec226b7`（Odoo wiring）/ `d3aa60e`（M.9 版號無關 test） |

分析對象（落地檔，非 test）：operator 端 `jwt_signer.py` / `agent_runtimes.py` / `main.py` / `config.py` / `webhook_dispatcher.py` / `mcp_invalidator.py` / `helm.py`(+2 method)；Odoo 端 `agent_runtime_skill.py` / `agent_runtime_webhook.py` / `agent_runtime_client.py` / `ai_assistant.py`(SSE) / `agent_session.py` / `migrations/18.0.1.0.27/` / `agent_session_cron.xml`。

---

## 2. 四面向結果

### 2.1 Security

> 委派：security-auditor sub-agent 獨立過一輪（含我預判讓它 verify/refute）；我對每條獨立覆核 + 自抓 1 條（C-5 namespace secret）。

**正向確認（無發現 / 守線）**：

- **[clean] HC-2 token authoritative（第三度落地）** — `agent_runtime_skill.py:101-110`：`project_id = claims.get('project_id')` 只取**已驗章 token claim**，query `project_hash`/`skill_name`/`version` 僅作非權威 filter，`project_hash` **從不**用於 project 解析。search domain `('project_id','=', claims['project_id'])` → token 為 project A 簽就**只能**撈 project A 的 skill，**無法跨 project**。對齊 [[constraint-input-trust-expansion]]。
- **[clean] HC-4 未被繞過** — skill-bundle endpoint 用 `sudo()` 讀 `skill.bundle_attachment_id.datas`（`agent_runtime_skill.py:106-118`），但這是 **machine-auth 獨立路徑**（HS256 shared secret + token project_id 為界），**不是** HC-4 守的 `/web/content` user-session 側通道。anchor stamping（`skill.py:108-129`）+ ir.attachment row-level deny rule 對 user 仍完整。`sudo()` 在此讀的是「token 自己 project 的 published bundle」，user 拿 session credential 到不了此 endpoint（需 shared signing secret）→ 合理，非回歸。
- **[clean] B1 project_access gate 在 spawn 之前** — `ai_assistant.py:577-580` `_check_project_access(project)` 在 `_stream_agent_runtime` 內、spawn-or-attach（`:610`）之前；順序 CSRF（`:374-376`）→ channel access（`:381-382`）→ flag gate（`:395`）→ project_access（`:577`）→ spawn。達成 design §2.13 AC#14，無 [[cross-workspace-task-data-leak]] 回歸。`invoker_role` 由 `_get_project_access_role` server-side 解析（`:591`），非 client 輸入 → 無 role injection。
- **[clean] resume token re-sign 無 race** — `agent_runtimes.py:70-84` `_resume_pod`：先 `sign_skill_bundle_token`（`:76`）→ `set_deployment_env`（`:81`）→ `scale_deployment(...,1)`（`:84`）。env patch **嚴格先於** scale-1，pod 重建即帶 fresh token，對齊 design §2.11.1「scale-1 之前 patch env」。
- **[clean] dual-secret 不放行 expired token** — operator `jwt_signer.py:162-173` 與 Odoo `agent_runtime_skill.py:68-79` 皆「先比對 signature（current 然後 previous），match 後才檢 exp」；expired 直接 reject，**不**因 previous secret 給第二次機會。
- **[clean] 無 command injection** — 全 repo `shell=True` 零命中；helm/kubectl 皆 `subprocess.run` list args，`set_deployment_env`（`helm.py:856-872`）把 token 當 literal argv token，JWT 無 shell metachar。
- **[clean] SSRF（本 diff）** — `webhook_dispatcher.dispatch_state_update` 的 `odoo_base_url` 來自 operator settings/env（`config.py:39`），非 request payload；§6.7 既有 minimal SSRF guard 繼承。

**caveat / info**：

- **[caveat — DISPUTED severity] C-1 token 無 explicit `scope`/`token_use` claim** — `jwt_signer.py:79-116`：`ws_token`（帶 channel_id+invoker、無 project_id）與 `skill_bundle_token`（帶 project_id+workspace_ids、無 channel_id）僅靠**隱性 claim shape** 區分，共用同把 HS256 secret、同 iss/aud。
  - **security-auditor 判 blocker**；**我判 caveat**。爭議裁決見 §7「Disputed severity 上呈」。
  - 我方理由（摘）：(1) skill-bundle endpoint 對 project scoping airtight，**不存在跨 project escalation**（auditor 標題的「cross-project」實不成立）；(2) cross-endpoint 混用（skill_bundle_token 拿去開 pod WS）由 **Phase B 跨 repo WS verifier** 守，且 skill_bundle_token 缺 channel_id；(3) design §6.6 line 3704-3707 **明文接受** v1 bearer token 1h 內可重放，且 §6.6 pod verify pseudocode（line 3740）**從未要求** scope claim → 非 plan 偏離。
  - 方向（無論裁決）：加 `token_use` claim（`"ws"`/`"skill_bundle"`）並在兩端 verifier assert — 廉價 defense-in-depth，關掉「shared secret + shape-only」的隱性耦合。
- **[caveat] C-2 operator auth middleware fail-open** — `main.py:89-91`：`settings.api_key == ""` 時 middleware 記 warning 後 `call_next`（**fail open**），全 endpoint 無鑑權。pre-existing pattern，但 Phase C 把 operator 變成**token 簽發者**（`agent_runtimes.py:95` 用 request body 的 `project_id` 簽 skill_bundle_token）→ fail-open 時攻擊者可對**任意 project_id** 取得合法 skill_bundle_token → 到 Odoo skill-bundle endpoint 下載該 project 的 published bundle（跨租戶 bundle 外洩原語，Phase C 前不存在）。方向：`api_key` 未設時 **fail closed**（拒啟動 / 503），至少對 agent-runtime router。
- **[caveat] C-3 webhook HMAC verify 缺 dual-secret fallback（偏離 design）** — `agent_runtime_webhook.py:62` 只讀單一 `AGENT_RUNTIME_WEBHOOK_SECRET`，無 current/previous loop。design §5.3 line 2676「webhook 同理」+ line 2690 註解「skill-bundle token verify + webhook HMAC verify 共用此 pattern」**明文要求** dual-secret。影響：rotation 窗口內舊 secret 簽的 webhook 被 401；cron 輪詢（§5.5）兜底使 state 最終收斂 → 可用性/一致性 gap，非安全洞。方向：補 current/previous loop，並對齊 env 名（operator signer + design line 2697 提到 `..._WEBHOOK_SECRET_CURRENT`）。
- **[caveat] C-4 webhook 無 replay / freshness 防護** — `agent_runtime_webhook.py:30-37, 82`：HMAC timing-safe（`hmac.compare_digest` ✓）但無 nonce / timestamp 窗口；body 的 `ts` 只 stamp `last_state_event_ts`，不驗新鮮度。被擷取的合法 webhook POST 可原樣 replay → 翻動 `agent_session.state`（cron 兜底自癒）。方向：用簽章涵蓋的 `ts` 拒收過舊事件，或 per project_hash 強制單調 `sequence_number`。
- **[caveat] C-5 namespace secret 硬編碼 fallback + 缺 auto-generation（plan gap，CAQA 自抓）** — `agent_session.py:11,122-127`：`_get_namespace_secret()` 在 `woow_paas_platform.agent_namespace_secret` 未設時回傳硬編碼公開值 `b'woow-paas-agent-namespace-fallback-secret'`。經查 `hooks.py`（`pre_init`/`post_init`/`uninstall` 全讀過）+ migrations **無任何隨機 secret 生成**（`secrets.token_*`/`os.urandom` 命中全在 OAuth/wallet，無一處生 namespace secret）。design §2.15 line 1344 把「secret leak → 可預測 hash → enumerate namespace」列 **HIGH**，mitigation 明寫「migration 第一次部署時自動生成 random」— **未實作**。
  - 影響評估：fresh install 若 admin 未手填該 param → silent 落到公開 fallback → namespace hash 可被讀原始碼者預測。**但**：(1) 只洩漏 namespace 命名（非 token 簽章 secret、非 webhook secret，皆獨立 env 注入）；(2) K8s RBAC 仍 gate 實際 namespace access（design §2.15「enumerate 到名稱也無 K8s read permission」）；(3) skill-bundle 用 project_id 解析非 hash → hash 可預測不助下載。→ 故判 **caveat**（plan-alignment gap + silent-misconfig 安全 smell），非 blocker。方向：補 design 既定的 post_init random secret 生成；或 fallback 命中時 log warn / fail loud。
- **[caveat] C-6 project_hash operator 邊界未驗格式** — `agent_runtimes.py:38` + `mcp_invalidator.py:18-35`：request body 的 `project_hash` 插入 namespace/release/label/configmap 名，`validate_namespace`（`helm.py:22-27`）只檢 `paas-ws-` 前綴、不檢 hash 尾。正常流程下 hash 由 Odoo `_compute_project_namespace_hash` 生（base32 lower，DNS-1123 安全），故實務安全；list-arg 調用排除 shell injection。但 operator 盲信 caller body（疊加 C-2 fail-open 放大）。方向：operator 邊界對 `^[a-z2-7]{16}$` 驗格式。
- **[info] C-7 exc 字串洩漏到 SSE client** — `ai_assistant.py:625`（`f'Pod spawn failed: {exc}'`）+ `:721-727`（catch-all `json.dumps({'error': str(exc)})` 串到 browser）。WS handshake 例外字串理論上可能含連線上下文（token 風險為 library-dependent，今未觀察到）。方向：client 端給泛化錯誤，`exc` 細節只進 `_logger`。

### 2.2 Quality

- **[clean] migration .27 idempotent + replayable** — `migrations/18.0.1.0.27/post-migrate.py:18-24`：`ALTER TABLE ... ADD COLUMN IF NOT EXISTS last_synced_at timestamp, last_state_event_ts timestamp`；additive + nullable + 無 backfill；欄位型態/可空性與 `agent_session.py:89-96`（兩個 `fields.Datetime`、皆非 required）精確相符。re-run no-op。既有 row 留 NULL，被 cron 視為 stale（`agent_session.py:143-146` `last_synced_at = False` 也算 stale）撿起。無 [[plan-vs-odoo-schema-mismatch]] 重蹈（phantom field=0）。
- **[clean] coverage 紮實** — test-coverage report 載明 52 method（pytest 24 + Odoo 28）跨 dual-system，frozen contract（第 4 章）凍結成對方期望，HC-2 第三度（SB.4/SB.4b）有 cell map。我方 caveat 群正好落在 **test 結構上抓不到的面向**（deploy-config fail-open、flag 後的 latent dep break、design-vs-impl 的 webhook secret、未實作的 auto-gen）→ 與 coverage 互補，非 coverage 漏洞。
- **[info] Q-1 `datetime.utcnow()` deprecated** — `ai_assistant.py:659`（py3.12 deprecation）；design §5.6 pseudocode 同。僅 flag-on path 觸發。方向：`datetime.now(timezone.utc)`。
- **[info] Q-2 HS256 verify 雙實作** — operator `jwt_signer._decode_verified`（`jwt_signer.py:120-137`）與 Odoo `_decode_verified_hs256`（`agent_runtime_skill.py:42-59`）邏輯重複。**屬刻意**（跨 process/repo 無法共享 module），非 DRY 違規；但兩處若日後一處改 algo 另一處漏改會 drift。方向：註解互指或抽 vendored 小檔（低優先）。
- **[info] Q-3 `_persist_ai_response` 簽名與 design 文字漂移但 impl 正確** — design §5.6 line 2941 末參傳 `template`；impl 傳 `context`（`ai_assistant.py:702-704`，def `:781`）。impl 才對（需 env context 重建 cursor，非 template）→ **plan 文字 stale，code 正確**，不需 escalate。
- **[info] Q-4 `new_state` 寫入無 Selection 預檢** — `agent_runtime_webhook.py:84-85` 直接把 `payload['new_state']` 寫 `state` Selection。Odoo `Selection.write` 會驗 enum 並對非法值 raise → out-of-enum 得 500（非 silent corruption）。方向（選配）：對 Selection set 預檢回 400，contract 更乾淨。

### 2.3 Performance

- **[caveat] P-1 SSE worker-pin** — `ai_assistant.py:635-739` `_stream_agent_runtime` 的 WS relay 在整段對話期間佔住 1 個 Odoo sync worker（generator 內 asyncio loop run）。**design §6.8 已認列此 tradeoff**（拒 §5.5 SSE-for-state 同理由），給公式 `required_workers = peak_concurrent_active_conv(50) + baseline(16) = 66`。`ping_interval=30/ping_timeout=60`（`:651-652`）bound 死 pod 情形。方向：Phase D 依此 size worker pool；並確保 pod 端 envelope 必以 `done`/`fatal` 收尾（relay 只在此退出 — 永遠串流的 pod 會釘死 worker）。
- **[caveat] P-2 async-gen 未 `aclose()`** — `ai_assistant.py:678-729`：`generate()` 建 `asyncio.new_event_loop()`，`finally` 只 `loop.close()`，未先 `loop.run_until_complete(agen.aclose())`。browser 中途斷線時 async generator 被 GC 而非乾淨關閉，`async with websockets.connect(...)` 的 `__aexit__` 可能不跑 → pod 側 WS 殘留至 ping_timeout（≤60s）。flag-gated，低急迫。
- **[info] P-3 cron drift 5min sequential** — `agent_session_cron.xml`（interval 5 min ✓ 對齊 design §5.5）+ `_cron_drift_detection`（stale >10min threshold ✓）。每 stale session 同步 `client.get_status`（30s timeout，sequential loop，`agent_session.py:147-162`）→ stale 數多時 cron 單輪可能久。webhook 為主、cron 為兜底（應少量）→ 低風險。operator 未配置時 `get_agent_runtime_client` 回 None early-return（`:138`）→ inert，不空轉。
- **[info] P-4 operator `verify_token` + jti cache 似未被 router 取用** — `jwt_signer.verify_token`（含 in-process jti cache，`:140-179`）在 `agent_runtimes.py` router 內**未見呼用**（operator 只簽、pod/Odoo 才驗）。docstring 自陳「operator 自己的 helper，unit-test 用；pod 自帶 verify」。故「operator scale >1 replica 時 in-process jti cache 失效」在本 diff **不成立**（該 path 未在請求路徑啟用）。屬可移除/待用的 helper（info）。

### 2.4 Architecture

- **[clean] operator 既有 router 完全不動** — `main.py:157-163`：`agent_runtimes.router` append 在既有六個（releases/namespaces/routes/tunnels/init/odoo_modules）之後，無一被改；新 router 只**消費**既有 `helm_service.install/get/upgrade/uninstall` + `k8s_service.scale/patch/get_pods`，未加新 helm primitive。對齊 design §2.11.2。
- **[clean] helm.py 2 method 純 additive** — `get_deployment_replicas`（`helm.py:837-854`）+ `set_deployment_env`（`:856-872`）為純新增，皆 `validate_namespace` 前綴守門，無既有 method body 改動。
- **[clean] WS flag default False（Phase C inert）** — `project_project.py:65` `use_agent_runtime = fields.Boolean(default=False)`；gate `ai_assistant.py:395` 僅 True 才入 pod branch。Phase C 不啟動 live pod 流量；SSE.1 test 鎖定 default。
- **[clean] cross-repo frozen contract 精確相符（priority check）** — Odoo `agent_runtime_skill.py` ⇄ Phase B `paas-agent-runtime/runtime/skills/cache.py` 逐欄位驗：

  | 契約元素 | Odoo endpoint | Phase B consumer | Match |
  |---|---|---|---|
  | URL path | `/api/agent-runtimes/skill-bundle`(`:87`) | `{base}/api/agent-runtimes/skill-bundle`(`cache.py:78`) | ✓ |
  | Query params | reads `skill_name`/`version`(`:108-109`)，`project_hash` 收而不用 | sends `project_hash`/`skill_name`/`version`(`cache.py:75-77`) | ✓ 名稱一致 |
  | Auth header | `Authorization: Bearer`(`:92-95`) | `Authorization: Bearer {token}`(`cache.py:79`) | ✓ |
  | Response body | **RAW bytes**(`make_response`,`:118-126`)；server 先 `base64.b64decode(attachment.datas)` | `resp.read()`→raw→`sha256`(`cache.py:81-88`) | ✓ raw（非 base64、非 JSON wrapper） |
  | Content-Type | `mimetype or 'application/gzip'`(`:122`) | `tarfile mode="r:gz"`(`cache.py:100`) | ✓ |
  | Status | 200 raw / 401 bad token / 404 unpublished | 200→ok / 任何 4xx5xx→`SkillFetchError` | ✓ |

  「raw bytes vs base64」陷阱處理正確：endpoint 在回應**前** b64-decode `attachment.datas` 成 raw，pod `resp.read()+sha256` 對的是同一份 raw bytes（SHA 算的也是這份）→ **無破壞 fetch/verify 的漂移**。
- **[caveat] A-1 `websockets.connect(extra_headers=)` latent break（plan-level，Phase D 必修）** — `ai_assistant.py:650` 用 `extra_headers=`；`requirements.txt:10` 釘 `websockets>=12.0`（**無上界**）。`extra_headers` 在 websockets 13 deprecated、**14 移除改名 `additional_headers`**（context7 驗證 12/14/16 版面）。裝到 ≥14 + flag 開 → relay 首次 connect 即 `TypeError` → 每條 flag-on 對話以泛化 SSE error 失敗。design §5.6 line 2887 **同樣寫 `extra_headers`** → **plan-level 缺陷被忠實複製**，非 impl 漂移。Phase C 不阻（flag default False + dep 註解為 install-on-activation）；但 Phase D 啟用前必修。方向：釘 `websockets>=12,<14` 或改 `additional_headers`（並更新 design）→ **escalate planner**（design 為源）。

---

## 3. Plan alignment check

Implementation 緊貼 §2.11 / §2.11.1 / §5.3 / §5.5 / §5.6 / §6.6：HS256 stdlib 雙 token、token-authoritative HC-2 skill-bundle、env-before-scale resume 順序、webhook-primary + cron-fallback、Bearer-header（非 query）token transport、dual-secret rotation 窗口（auth path）。`ai_assistant.py` SSE branch 幾乎逐行對齊 §5.6 pseudocode（含 gate 順序、pre-fetch-ORM-before-generator cursor pattern）。

**偏離 / gap（3 條，全已落 caveat）**：
1. **C-3**：webhook HMAC verify 略去 design §5.3 明文要求的 current/previous dual-secret loop。
2. **C-5**：design §2.15 規定的「migration 首次部署自動生成 namespace random secret」**未實作** → silent 落硬編碼公開 fallback。
3. **A-1**：design §5.6 與 impl 同用 websockets `extra_headers`（≥14 已移除）— plan-level latent defect。

**plan 文字 stale 但 impl 正確（不需 escalate）**：Q-3（`_persist_ai_response` 傳 context 非 template）；SSE「no project / flag False」走 legacy fall-through（design pseudocode 寫 mandatory `no_project`，impl 的 dual-mode fall-through 才對，否則 legacy channel 全壞 — SSE.4/SSE.5 鎖定）。

**design 從未要求、故非偏離**：C-1 scope claim（§6.6 pod verify pseudocode 無 scope claim；§5.3 原為單 token，雙 token split 是 §2.11.1 的 impl 精煉）。

---

## 4. 同源 code path 檢查

- **dual-secret verify pattern**：3 個實作 — operator `verify_token`（sig+exp+jti，`jwt_signer.py:153`）、Odoo `_verify_skill_bundle_token`（sig+exp，**刻意無 jti** — 對 per-pod-lifetime token 正確，`agent_runtime_skill.py:62`）、webhook `_verify_hmac`（**單 secret，C-3 odd-one-out**，`agent_runtime_webhook.py:30`）。前兩者皆「exp 在 signature match 之後」→ 無 expired-token-second-chance bug。**一個 pattern 對兩處、漏一處（webhook）**。
- **exp-after-signature**：`jwt_signer.py:172` + `agent_runtime_skill.py:77` 一致正確；webhook 無 exp 概念（HMAC body only）。
- **`auth='none' + sudo()` machine-auth endpoint**：`agent_runtime_skill.py` 與 `agent_runtime_webhook.py` 兩者都 crypto-verify 後才 sudo，且都從**已驗 credential**（token claim / project_hash）解權威 id 而非 untrusted param → pattern 一致。
- **`project_hash` 未驗插值**：`agent_runtimes.py:38` 與 `mcp_invalidator.py:18-35` 同缺格式驗（C-6 涵蓋兩處）。
- **`websockets.connect(extra_headers=)`**：全 addon 僅 `ai_assistant.py:650` 一處真正 call site（`n8n_init.py`/`mcp_server.py` 的 `extra_headers` 是無關 local 變數）→ A-1 defect 孤立未擴散。
- **硬編碼 secret fallback**：`agent_session.py:11` 一處；grep 確認無其他 model 有類似 namespace-secret fallback。

---

## 5. Known pitfalls 對照

| Pitfall / pattern | 對照結果 |
|---|---|
| [[constraint-input-trust-expansion]]（HC-2/HC-3 trust boundary）| **未回歸** — skill-bundle 用 verified token claim `project_id`（`agent_runtime_skill.py:102`），絕不信 attacker-supplied `project_hash` query。本 diff 無 attacker-writable m2m 推導 authority。 |
| [[attachment-row-level-acl-and-anchor-stamping]]（HC-4）| **未繞過** — `sudo()` 是 machine-auth 獨立路徑（shared secret + token project_id 為界），非 user-session `/web/content` 側通道；anchor stamping + ir.attachment deny rule 對 user 完整未動。 |
| [[cross-workspace-task-data-leak]]（B1）| **未重蹈** — `_check_project_access`（`ai_assistant.py:43-58`）鏡像 `mcp_server_rule_workspace_user` dual-leg，且 gate 在 spawn 之前；附 in-code 註解警告勿背離。 |
| [[plan-vs-odoo-schema-mismatch]] | **未重蹈** — migration .27 欄位（`last_synced_at`/`last_state_event_ts`）與 `agent_session.py:89-96` 精確相符，無 phantom field。 |
| CI container missing-node（cross-repo CI infra）| **不適用** — 本 diff 無動 Gitea Actions workflow；Phase B repo 的 CI 不在本 diff scope。 |

---

## 6. Blocker 清單（給 leader / implementation）

**我方判定：0 blocker。**（cross-repo contract 無漂移；無 data-integrity / 可利用安全洞 / 無 migration 的 breaking change。整段 flag-gated inert。）

⚠ 唯一須 leader 拍板的 severity：**C-1 token scope claim**（security-auditor 判 blocker、我判 caveat）— 見 §7。若 leader 採 blocker，本清單轉為 1 blocker + verdict→BLOCKED。

---

## 7. Disputed severity 上呈 leader（C-1 token scope claim）

| | security-auditor sub-agent | agent-runtime-pod-caqa（我） |
|---|---|---|
| 判定 | **blocker** | **caveat（medium）** |
| 核心論點 | 無 scope claim + 單 shared secret + jti-less 長壽 skill_bundle_token → leaked token 為「全可重放 bearer cred、無 audience binding」 | 同意「無 scope claim」屬實，但不構成 Phase C 可利用高危洞 |

**我判 caveat 的嚴格理由**：
1. **無跨 project escalation**：skill-bundle endpoint 用簽進 token 的 `project_id` 解析（攻擊者無 secret 改不了），project A 的 token **撈不到** project B → auditor 標題「cross-project」實不成立。auditor 自己也承認「跨 project bytes 暴露是 per-pod 長壽 bearer 在單 secret 下的本性，且 §6.6 部分接受」。
2. **cross-endpoint 混用由 Phase B 守**：skill_bundle_token 拿去開 pod WS 的疑慮，落在 **跨 repo 的 pod WS verifier**（Phase B image，本 diff 不可見），且 skill_bundle_token 缺 channel_id+invoker — 正確的 WS handler 應 reject。
3. **design 接受此風險、且從未要求 scope claim**：§6.6 line 3704-3707 明文「v1 接受 [replay-within-1h] risk（trust model + 1h exp 期短）」；§6.6 pod verify pseudocode（line 3740）只 decode+jti+exp，**無 scope assertion** → 故非 plan 偏離。
4. **真正放大跨 project 風險的是 C-2 fail-open**（無鑑權時可為任意 project 簽 properly-scoped token），scope claim 對該情境**無解**（攻擊者拿到的是目標 project 的合法 scoped token）→ 證明 C-1 與跨 project 風險正交。

**兩案共同 follow-up**：加 `token_use` claim 並在兩端 assert（廉價 hardening）；**並請 Phase B 確認 pod WS verifier 對缺 channel_id 的 token 一律 reject**（cross-repo 守線）。

> leader 裁決請回填本節；reviewer 若採 blocker 觀點，可在 PR review 以「hardening before Phase D」帶入。

---

## 8. Caveat 清單（給 leader 決定是否帶入 reviewer / Phase D gate）

| # | Sev | 位置 | 摘要 | 方向 |
|---|---|---|---|---|
| C-1 | caveat ⚠disputed | `jwt_signer.py:79-116` + `agent_runtime_skill.py:62-104` | 無 `token_use`/`scope` claim，ws vs skill_bundle 僅隱性 shape 區分 | 加 `token_use` claim 兩端 assert（見 §7） |
| C-2 | caveat | `main.py:89-91` | auth middleware `api_key` 未設時 fail-open；Phase C 使 operator 成 token 簽發者放大 blast radius | `api_key` 未設 → fail closed / 503 |
| C-3 | caveat（plan 偏離）| `agent_runtime_webhook.py:62` | webhook HMAC verify 單 secret，缺 design §5.3 要求的 current/previous fallback | 補 dual-secret loop + 對齊 env 名 |
| C-4 | caveat | `agent_runtime_webhook.py:30-37,82` | webhook 無 replay/freshness 防護，合法 POST 可重放翻 state（cron 自癒）| 用簽章 `ts` 拒舊事件 / 單調 sequence_number |
| C-5 | caveat（plan gap）| `agent_session.py:11,122-127` + `hooks.py` | namespace secret 硬編碼公開 fallback + design §2.15 規定的 auto-gen 未實作 | 補 post_init random secret 生成 / fallback 命中 fail-loud |
| C-6 | caveat | `agent_runtimes.py:38`,`mcp_invalidator.py:18-35` | operator 盲信 request body `project_hash`，未驗格式（疊 C-2 放大）| 邊界驗 `^[a-z2-7]{16}$` |
| A-1 | caveat（plan-level，Phase D 必修）| `ai_assistant.py:650` + `requirements.txt:10` | `websockets.connect(extra_headers=)` 在 websockets ≥14 已移除；釘 `>=12.0` 無上界 | 釘 `<14` 或改 `additional_headers`（更新 design）→ escalate planner |
| P-1 | caveat | `ai_assistant.py:635` | SSE relay 每 active conversation 佔 1 Odoo worker（design §6.8 認列）| Phase D 依公式 size worker pool=66 + 確保 pod 必 `done`/`fatal` 收尾 |
| P-2 | caveat | `ai_assistant.py:678-729` | async-gen 未 `aclose()`，client 斷線後 pod WS 殘留至 ping_timeout | drain loop 補 `aclose()` |

info（6）：C-7（exc 洩漏 SSE）、Q-1（`utcnow` deprecated）、Q-2（HS256 verify 雙實作）、Q-3（`_persist_ai_response` 簽名 plan stale）、Q-4（`new_state` 無 Selection 預檢→500）、P-3（cron sequential get_status）、P-4（operator `verify_token` 似未被 router 取用）。

---

## 9. 工具 raw 輸出摘要

- **Bash `git merge-base HEAD origin/develop`** → `9479cb2`；確認 `develop..HEAD` 不可用（本地 develop behind 20），改 `9479cb2..HEAD` 取乾淨 27-檔 Phase C diff。
- **security-auditor sub-agent**（26 tool_uses）：1 blocker（C-1，我降為 caveat 並上呈）+ 6 caveat + 1 info；verify/refute 我 4 預判全中（fail-open 確認、webhook 單 secret 確認、skill-bundle 無 jti 確認為正確設計、scope claim 確認）；`shell=True` 全 repo 零命中。
- **architect-reviewer sub-agent**（26 tool_uses）：cross-repo contract **逐欄位精確相符**（priority check 過）；operator router/helm additive 確認；新抓 A-1 websockets `extra_headers` latent break（context7 驗 websockets 12/14/16）；migration .27 idempotent 確認。
- **CAQA 自抓**：C-5 namespace secret 硬編碼 fallback + 缺 auto-gen（`hooks.py` 全段 + migrations grep 無隨機生成）；cron interval=5min 對齊 §5.5；§6.6 line 3704-3707 證實 v1 bearer replay 風險為設計接受（支撐 C-1 降級判斷）。
- **未做**：未 re-run test suite（impl + tester 已報 green：pytest 24 + Odoo 28 + 全 regression；CAQA 不重跑，只審 impl 對 design/invariant 的一致性）；未讀 operator `test_agent_runtimes.py` 613 行逐 case（test 正確性歸 reviewer/test-explainer）。

---

## 10. Hand-off

- **給 leader**：(1) 裁決 §7 C-1 severity（blocker / caveat）→ 決定 verdict 終值；(2) 決定 C-2~C-6 + A-1 + P-1/P-2 哪些帶入 reviewer、哪些列 Phase D gate / follow-up。
- **escalate planner**（經 leader）：A-1 design §5.6 `extra_headers`、C-3 webhook dual-secret「同理」未落地 — design 文字為源，需同步修。
- **cross-repo 守線**：請 Phase B 確認 pod WS verifier 對缺 channel_id 的 token reject（C-1 follow-up）。
- **建議 durable**：C-3 / C-4 / C-5 / A-1 / P-1 進 `docs/tracking/followups/follow-ups.md`（Phase C cycle）。
