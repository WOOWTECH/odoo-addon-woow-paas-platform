---
name: test-coverage-agent-runtime-phase-d-2026-05-24
phase: red-explained
team: feature-agent
test_explainer: agent-runtime-pod-test-explainer
plan_ref: docs/plans/2026-Q2/agent-runtime-pod-plan.md
design_ref: docs/plans/2026-Q2/agent-runtime-pod-design.md
tester_commit: 5398f92
created: 2026-05-24
updated: 2026-05-25
revisions:
  - 2026-05-25 §10 forward-coupling 措辭校正（impl green 跑全套：既有 session-creating test 5→3 class，由「tester 已預先設」改為「impl 需補設」；補 phase_c_client_webhook 3 建立點）
related:
  - docs/archived/tracking/test-coverage-agent-runtime-phase-a-2026-05-22.md
  - docs/archived/tracking/test-coverage-agent-runtime-phase-c-2026-05-24.md
  - docs/decisions/patterns/constraint-input-trust-expansion.md
  - docs/tracking/followups/follow-ups.md
status: archived
archived-from: docs/tracking/test-reports/test-coverage-agent-runtime-phase-d-2026-05-24.md
archived: 2026-07-19
archived-reason: "一次性測試報告，對應功能已上線"
---

# Test Coverage Report — Agent Runtime Pod Phase D（人類可讀版）

> 這份報告**不是** plan caveat 的逐條翻譯（plan 是 source-of-truth）。
> 它解釋：**「Phase D 為何這樣測、哪些今天綠是 by design、為何 F-PC-6 是 2 紅 + 1 guard、為何 F-PC-1/charts 要落 host」**，餵給 reviewer / CAQA / stakeholder 對齊測試意圖。
>
> **Phase D 是 enable-gate「code-ready」cycle** — close 掉 flip `use_agent_runtime` flag 的所有 CODE 前提（修 runtime/security/leak 缺陷 + 備好 admin 啟用面 + cross-repo wiring）。**不 flip flag、不起 pod、不部署、不做 2 週觀察**（那塊是 user-run runbook）。所以這批 test 全是 **caveat-driven hardening**，不是新功能 spec。
>
> **reviewer special attention**：
> 1. 第 2 章 **RED vs regression-guard 分類** — 14 條 RED（驅動 impl）vs 6 條 today-green guard（必須 STAY GREEN，破了就是 regression）。reviewer 對兩類的應對相反。
> 2. 第 4 章 **執行環境分流** — 同一批 red 跨 5 個 runner（Odoo in-container / paas-operator pytest / host unittest / charts bash / cross-repo）。**F-PC-1 + charts 為何「必須」落 host，不能在 docker 內跑**。
> 3. 第 5 章 **F-PC-6 = 2 紅 + 1 guard** — plan 原框「done/error/例外三路徑皆 leak」，tester 實證**只 done/error 兩路徑 leak**；例外路徑 Python 自動 unwind `async with` 已關 → 寫成紅會是**假紅**，故落成 green regression-guard（failure-mode honesty）。
> 4. 第 6 章 **F-PC-7 邊界**（WS2 cross-repo，本 explainer 看不到該 repo 檔，輸入由 paas-agent-runtime agent 盤好）— 為何單一紅（缺 `token_use` claim 應拒）+ 繼承綠足夠；泛用未知值為何留 green 階段。
> 5. 第 7 章 **frozen contract**（5 個凍結 env 名 + demo template xmlid）— tester 硬編這些名，impl 必須逐字對上，否則 charts mount / 讀取點 / test 三方錯位。

---

## 1. TL;DR

Phase D 把 Phase A+B+C（已 ship，PR #59 merged）留下的 **enable-gate must-fix** 全 close。涵蓋 **11 條 caveat 條件**，落在 5 個執行面：

| caveat | 一句話缺陷（今天紅的原因） | 落點 | runner |
|--------|--------------------------|------|--------|
| **F-PC-1**（A-1）| `requirements.txt` websockets 無 `<14` 上界 → ≥14 改名 `extra_headers`→`additional_headers`，bridge runtime 炸 | floor-root `requirements.txt` | **host** unittest |
| **F-PC-2**（C-3）| webhook receiver 只讀 current secret → previous-secret 簽的 body 被 401（rotation overlap 斷） | `agent_runtime_webhook.py` | Odoo HttpCase |
| **F-PC-3**（C-5/D-2）| namespace hash 用 source-public `_DEFAULT_SECRET` fallback；缺 env 不 fail-closed | `agent_session.py` | Odoo TransactionCase |
| **F-PC-4**（C-2/D-1）| operator `api_key=""` → 所有受保護 route pass-through（fail-OPEN） | `paas-operator/src/main.py` | **pytest** |
| **F-PC-5**（N-1）| operator 4xx/5xx 時 `_request` 不拋 typed error（吞錯誤 dict） | `agent_runtime_client.py` | Odoo TransactionCase |
| **F-PC-6**（P-2）| SSE→pod WS bridge 在 done/error 分支 `break` 留 generator suspended，未 `aclose()` → 每輪對話洩 pod 連線 | `ai_assistant.py` | Odoo HttpCase |
| **F-PA-14**（HC-2/D-3）| `sudo().create()` 帶 owner hint 仍跳 bootstrap → 真人 owner 無 `project_access` row | `project_project.py` | Odoo TransactionCase |
| **charts**（OQ#1 b）| `helm template` odoo-deployment 缺凍結 5 env | `charts/odoo-dev-sandbox` | **host** bash + helm |
| **UI.1**（WS3）| project form view 不含 `use_agent_runtime` toggle → admin 無法從 UI 啟用 pilot | （新）`project_views.xml` | Odoo TransactionCase |
| **SEED.1**（WS3）| demo `agent_template` seed record 不存在（xmlid 不解析） | （新）`agent_template_demo.xml` | Odoo TransactionCase |
| **F-PC-7**（WS2）| paas-agent-runtime verifier 缺 `token_use` claim 仍放行（A 嚴格應拒） | `paas-agent-runtime` repo | cross-repo pytest |

**對應 test：7 個 test 檔（本 repo）+ 1 條 cross-repo。** 方法級盤點（見第 12 章校正）：

| 類別 | 數 | 含義 |
|------|---:|------|
| **RED（驅動 impl，今天紅）** | **14** | in-repo 13（Odoo 10 + operator 1 + charts 1 + host 1）+ WS2 F-PC-7 1 |
| **regression-guard（今天綠，必須 STAY GREEN）** | **6** | F-PC-2 current·unknown · F-PC-4 configured-401·health-ok · F-PA-14 no-hint · F-PC-6 exception |
| **繼承綠（Phase C 已綠續綠，非本 session 新增）** | — | F-PC-7 的 `token_use=ws` 通過 + `skill_bundle` 拒（Phase C C-1，commit 1305b77/7daf1f4） |

> **「11 條」vs「14 RED 方法」不矛盾**：11 是 caveat **條件**數（brief / plan 口徑），14 是 **test 方法**數（F-PC-3/5/6 各拆 2 紅，UI/SEED 各 1）。commit title「10 caveat guards」= 10 條 in-repo 條件；commit「Odoo 10 fail of 710」= 10 條 Odoo-side RED 方法。三個數字全可對齊 → 第 12 章逐項校正。

**Gate 答覆（「為何測這些、為何不測那些」）**：
- **測這些** = flip flag 前會「炸 / 漏 / 越權」的 7 條 must-fix + 啟用面 2 條 + cross-repo 1 條 + charts mount 1 條。每條都有明確的 production 失效模式（runtime TypeError / 連線洩漏 / 跨租戶 token / fail-open K8s 操作 / namespace 公開可算）。
- **不測那些** = 需要 live pod 才能驗的（實機 WS connect、spawn pod 端到端、SLO metric）全列 **pilot-gate re-verify**，不在本 session 單測（無 pod 無法單測，見第 9 章）。

---

## 2. RED vs regression-guard — 分類與 reviewer 應對

Phase D 每條 caveat 的 test 都成對出現：**紅** 驗「修好後新行為對」，**guard** 鎖「修的時候不准弄壞既有正確行為」。reviewer 對兩類的應對**相反**，這是讀本批 diff 最關鍵的一張表。

| caveat | RED（impl 後應轉綠） | regression-guard（全程 STAY GREEN） | guard 守的既有行為 |
|--------|---------------------|-------------------------------------|-------------------|
| F-PC-1 | `test_websockets_has_upper_bound_below_14` | —（純缺陷修補，無對稱既有行為） | — |
| F-PC-2 | `test_FPC2_previous_secret_signed_accepted` | `test_FPC2_current_secret_still_accepted`、`test_FPC2_unknown_secret_rejected` | current-secret 仍 200；亂簽仍 401（dual-env 不可放寬成「什麼都收」） |
| F-PC-3 | `test_FPC3_hash_uses_env_secret_not_default`、`test_FPC3_missing_env_fails_closed` | —（forward-compat 由既有 session-creating test 承擔，見第 10 章） | — |
| F-PC-4 | `test_unset_api_key_fails_closed_on_protected_routes` | `test_configured_key_missing_header_still_401`、`test_unset_api_key_health_still_ok` | 正常 key+缺 header 仍 401（非 503）；`/health` 仍免認證可達（k8s liveness） |
| F-PC-5 | `test_FPC5_4xx_raises_with_operator_detail`、`test_FPC5_5xx_raises_with_status` | —（既有「成功路徑回 dict」由 Phase C client test 守） | — |
| F-PC-6 | `test_FPC6a_done_branch_closes_connection`、`test_FPC6b_error_branch_closes_connection` | `test_FPC6c_exception_branch_already_closes` | 例外路徑本來就 unwind `async with` 關掉 socket，aclose() fix 不准 regress（見第 5 章） |
| F-PA-14 | `test_FPA14_sudo_create_with_hint_bootstraps_owner` | `test_FPA14_sudo_create_without_hint_no_auto_owner` | 無 hint 的 SUPERUSER create **不准**自動讓 `__system__` 成 owner（HC-2 invariant，見第 8 章） |
| UI.1 | `test_UI1_project_form_arch_exposes_use_agent_runtime` | —（precondition 斷言 Phase A field 存在，非 guard） | — |
| SEED.1 | `test_SEED1_demo_agent_template_exists_unpublished` | — | — |
| F-PC-7 | （WS2）缺 `token_use` claim → 拒 `TokenUseError` | （繼承綠）`token_use=ws` 通過、`skill_bundle` 拒 | 合法 ws token 不可被新嚴格度誤殺；跨 scope token 仍拒（見第 6 章） |

**reviewer 應對**：
- **RED 列** → impl 後**必須轉綠**。仍紅 = 該 caveat 沒修完。
- **guard 列** → impl 前後**都綠**。若 impl 後變紅 = **改壞了既有正確行為**（regression），比 RED 沒轉綠更嚴重 —— 這正是 guard 的存在價值（把「順手改壞」擋在 merge 前）。

---

## 3. caveat → test 對應（cell-map）

每條 caveat 接到哪個 test 方法、紅在哪個斷言、對應 plan/design 哪一節、為何選這個邊界。**explicit** = 直接驗該行為；**guard** = 鎖既有不變。

### 3.1 WS1 — 本 repo Odoo addon（in-container suite）

| caveat | test 方法 | file:line | 紅斷言入口 | plan/design ref | 為何選這個邊界 |
|--------|----------|-----------|-----------|-----------------|----------------|
| F-PC-2 | `test_FPC2_previous_secret_signed_accepted` (explicit) | `src/tests/test_agent_pod_phase_d_hardening.py:131` | previous-secret 簽 body → 期望 200（今 401） | §6.6 rotation overlap；複用 `agent_runtime_skill.py:62-80` dual-env pattern | webhook secret 輪替時必有「current+previous 並存」窗口；只測 previous 被收=直接驗 rotation 不斷線 |
| F-PC-2 | `test_FPC2_current_secret_still_accepted` (guard) | `:142` | current 簽 → 200 | 同上 | 防 dual-env「改成只認 previous」 |
| F-PC-2 | `test_FPC2_unknown_secret_rejected` (guard) | `:149` | 亂簽 → 401 | 同上 | 防 dual-env「放寬成什麼都收」 |
| F-PC-3 | `test_FPC3_hash_uses_env_secret_not_default` (explicit) | `:168` | hash 須由 `AGENT_RUNTIME_NAMESPACE_SECRET` env 算，非 `_DEFAULT_SECRET` | C-5/D-2，OQ#1 ruling(b) | 直接驗「env 是 secret 唯一來源」；同時把 legacy ICP param 設空證明不靠它 |
| F-PC-3 | `test_FPC3_missing_env_fails_closed` (explicit) | `:192` | 缺 env → `create` raise（今用公開 fallback silent 算） | 同上 + §2.15（待 follow-up 改寫） | 缺 secret 必 fail-closed，否則任何讀 source 的人能算出 namespace hash（公開原語） |
| F-PC-5 | `test_FPC5_4xx_raises_with_operator_detail` (explicit) | `:233` | 401 → 拋 typed error 且帶 operator `detail` | N-1，§5.6 error surface | 不只「有拋」，要驗 detail 被攜帶（否則 SSE 端 surface 出 cryptic 真因 = 半修） |
| F-PC-5 | `test_FPC5_5xx_raises_with_status` (explicit) | `:247` | 500 → 拋且帶 status/detail | 同上 | 4xx（client）與 5xx（server）兩類錯誤都要 typed，邊界各取一 |
| F-PA-14 | `test_FPA14_sudo_create_with_hint_bootstraps_owner` (explicit) | `:283` | sudo+`bootstrap_owner_partner_id` hint → 該 partner 得 owner row | HC-2/D-3，`[[constraint-input-trust-expansion]]` | 驗「可信 sudo 呼叫者明示 owner」這條窄路徑能 bootstrap |
| F-PA-14 | `test_FPA14_sudo_create_without_hint_no_auto_owner` (guard) | `:297` | 無 hint 的 sudo create → `__system__` 無 owner row | 同上 | 鎖 HC-2 invariant：fix 必須是 hint，不是 blanket sudo-bootstrap（見第 8 章） |
| F-PC-6 | `test_FPC6a_done_branch_closes_connection` (explicit) | `src/tests/test_agent_pod_phase_d_sse_aclose.py:218` | done envelope 後 fake WS conn `.closed` 為 True（今 False） | P-2，`ai_assistant.py:678-729` | done 分支 break 留 generator suspended（見第 5 章） |
| F-PC-6 | `test_FPC6b_error_branch_closes_connection` (explicit) | `:233` | error envelope 後 `.closed` 為 True | 同上 | error 分支同樣 break-leak |
| F-PC-6 | `test_FPC6c_exception_branch_already_closes` (guard) | `:247` | iteration raise → `.closed` 為 True（今天已綠） | 同上 | 例外路徑本就 auto-unwind；鎖 aclose() fix 不 regress（見第 5 章） |
| UI.1 | `test_UI1_project_form_arch_exposes_use_agent_runtime` (explicit) | `src/tests/test_agent_pod_phase_d_enablement.py:44` | resolved form arch 含 `use_agent_runtime`（先斷言 field 存在=precondition） | WS3，field 在 `project_project.py:63` Phase A ship | 驗「admin 從 UI 能看到 toggle」；precondition 把「field 缺」與「view 沒顯示」分離（紅指 view，非 schema） |
| SEED.1 | `test_SEED1_demo_agent_template_exists_unpublished` (explicit) | `:68` | xmlid `woow_paas_platform.agent_template_demo` 解析、是 agent_template、且 `state != 'published'` | WS3 | 三重斷言把「存在」「型別對」「未發布」鎖死 → frozen contract（見第 7 章） |

### 3.2 WS1 — paas-operator（pytest，mock，非 Odoo）

| caveat | test 方法 | file:line | 紅斷言入口 | ref | 為何選這個邊界 |
|--------|----------|-----------|-----------|-----|----------------|
| F-PC-4 | `test_unset_api_key_fails_closed_on_protected_routes` (explicit) | `extra/paas-operator/tests/test_auth_fail_closed.py:54` | `api_key=""` 打 5 條代表性受保護 route → 全 503（今 pass-through 非 503） | C-2/D-1 | middleware 在 routing **之前**跑，故 5 條 route 不需真存在 — 探的是 global gate，不是 per-route（見第 9 章邊界） |
| F-PC-4 | `test_configured_key_missing_header_still_401` (guard) | `:71` | 設真 key + 缺 header → 401（非 503） | 同上 | 鎖「fail-closed 只對 UNSET key 生效」，不誤傷正常 bad-credential 流程 |
| F-PC-4 | `test_unset_api_key_health_still_ok` (guard) | `:89` | `api_key=""` 時 `/health` 仍 200 | 同上 | 鎖 fail-closed 不打死 k8s liveness probe（health/docs 豁免） |

### 3.3 WS1 — host runner（docker 不可達，見第 4 章）

| caveat | test 方法 | file:line | 紅斷言入口 | ref | 為何選這個邊界 |
|--------|----------|-----------|-----------|-----|----------------|
| F-PC-1 | `test_websockets_has_upper_bound_below_14` (explicit) | `tests/test_phase_d_packaging.py:69` | `requirements.txt` 的 websockets 釘有 `<14`（regex 解析）且上界 ≤14 | A-1，`ai_assistant.py:648-650` | 只測「pin 有上界」這個**靜態可驗**面；實機 connect kwarg 相容 → pilot gate（無 pod 無法單測，見第 9 章） |
| charts | `test_agent_runtime_env.sh`（bash 斷言，無 method） | `charts/odoo-dev-sandbox/tests/test_agent_runtime_env.sh` | `helm template -s odoo-deployment.yaml` render 出凍結 5 env，任一缺=exit 1 | OQ#1 ruling(b)，軟契約 env 表 | 純 `helm template` + grep，無 helm-unittest 依賴；scope 到 odoo-deployment（5 secret 屬 Odoo container） |

### 3.4 WS2 — cross-repo（paas-agent-runtime，本 explainer 看不到該 repo 檔）

| caveat | test（輸入由 paas-agent-runtime agent 盤好） | 落點 | 紅斷言 | ref | 為何充分（見第 6 章） |
|--------|---------------------------------------------|------|--------|-----|---------------------|
| F-PC-7 | 缺 `token_use` claim → 拒 `TokenUseError` (explicit) | `paas-agent-runtime` `runtime/transport/auth.py verify_token`（B transitional，commit `18d9602`） | 缺 claim 現放行（A 嚴格應拒） | 對稱 Odoo `agent_runtime_skill.py:105`（已 A 嚴格）| 配繼承綠（ws 通過 + skill_bundle 拒）涵蓋「正向放行 / 反向拒絕 / 缺欄位拒絕」三面 |

---

## 4. 執行環境分流 — 同一批 red 跨 5 個 runner

Phase D 的 red 不在單一 suite。reviewer 跑驗證 / 看 CI 時必須知道**哪條落哪個 runner**，尤其 **F-PC-1 + charts 為何不能在 docker Odoo 容器內跑**。

| runner | 條目 | 怎麼跑 | 為何在這裡 |
|--------|------|--------|-----------|
| **Odoo in-container suite**（710 tests） | F-PC-2 / F-PC-3 / F-PC-5 / F-PC-6 / F-PA-14 / UI.1 / SEED.1 | `--test-tags woow_paas_platform`（10 RED + 4 guard 方法） | 測 addon model / controller / view，需 Odoo registry + DB |
| **paas-operator pytest** | F-PC-4 | `cd extra/paas-operator && pytest tests/test_auth_fail_closed.py`（1 RED + 2 guard） | FastAPI middleware，mock settings，與 Odoo 不同 process/runtime |
| **host unittest** | F-PC-1 | `python3 -m unittest tests.test_phase_d_packaging`（1 RED） | **見下方分流理由** |
| **host bash + helm** | charts | `HELM_BINARY=$(which helm) bash charts/odoo-dev-sandbox/tests/test_agent_runtime_env.sh`（1 RED） | **見下方分流理由** |
| **cross-repo pytest** | F-PC-7 | paas-agent-runtime repo（獨立 agent） | 不同 repo，本 explainer 不可見 |

### 為何 F-PC-1 必須落 host

docker dev/test setup 只把 `./src` mount 到 `/mnt/extra-addons/woow_paas_platform`（見 `docker-compose.yml`）。F-PC-1 要驗的 `websockets` pin 在 **floor-root `requirements.txt`**，**不在那個 mount 內** → 容器內 Odoo test **讀不到該檔**。test 用 `_floor_root()`（檔案上一層）定位 `requirements.txt`，這只有在 host 上跑才成立。fixture 已防呆：找不到檔會明說「run this on the HOST, not inside the docker container」，不會誤判成 feature red。

### 為何 charts 必須落 host

charts 斷言靠 `helm template` render，需要 **host 上的 helm binary**（`HELM_BINARY=$(which helm)`）；容器內無 helm。腳本對「helm 找不到」(exit 2)、「render 失敗」(exit 2) 與「env 缺」(exit 1=真紅) 三態分流 —— exit 2 是 harness 問題不是 feature red，reviewer 看到 exit 2 不該當成 charts 沒修。

> **小結**：F-PC-1 / charts 落 host 是**環境限制**（mount 邊界 + 工具依賴），不是 tester 偷懶。CI 若只跑 in-container suite 會**漏掉這 2 條** → 第 11 章提醒 reviewer 手動補跑。

---

## 5. F-PC-6 = 2 紅 + 1 guard（failure-mode honesty）

這是本批最需要解說的一條。**plan 原框**（§WS1 F-PC-6）說 bridge「done / error / 例外 三路徑」relay 都未 aclose。**tester 靜態分析後實證：只有 done / error 兩路徑真的 leak**，例外路徑不 leak —— 所以寫成 3 紅會有 1 條是**假紅**。

### 缺陷機制（為何 done/error leak）

`ai_assistant.py:678-729` 用 `loop.run_until_complete(agen.__anext__())` 逐 frame drain `relay()` async generator。done / error 分支收到對應 envelope 後 **`break`** 跳出 while loop —— 此時 `agen` 停在它的 `yield`（仍在 `async with websockets.connect()` 內 **suspended**）。`finally` 只 `loop.close()`，**從不 `agen.aclose()`** → connection 的 `__aexit__`（WS close）永不執行 → **每輪對話洩一條 pod 連線**。

### 為何例外路徑是 guard，不是紅

當 iteration 自身 raise（`__anext__` 拋例外），Python **原生 unwind** `async with` → `__aexit__` 跑掉 → socket **今天就關了**。故 `test_FPC6c_exception_branch_already_closes` **今天綠**，且 aclose() fix 上線後**仍須綠**（aclose() 對已 finalize 的 generator 是 harmless no-op）。把它寫成「紅」= 宣稱一個不存在的缺陷 = **假紅** → tester 正確地落成 **green regression-guard**，鎖「fix 不准 regress 這條本就正確的路徑」。

### 觀察手法（為何這紅可靠、不靠 GC timing）

test 注入 fake `websockets` module，其連線 `_FakeWSConn.__aexit__` 內 `await asyncio.sleep(0)` 後才設 `closed=True` —— 模擬真 WS close **需 live event loop 才能 await 完成**。沒有 aclose() fix 時：drain 在 generator suspended 下 break、`loop.close()`、被遺棄的 generator 只能等 GC 才 finalize，**那時 loop 已關 → 這個 await 跑不完 → `closed` 永遠 False**（=可觀察的 leak）。有 fix 時：`loop.run_until_complete(agen.aclose())` 趁 loop 還開著 unwind → await 完成 → `closed` 確定性變 True。**這把「洩漏」變成 deterministic 布林斷言，不依賴 GC 時機**，紅得乾淨。

> **對 reviewer**：impl 只需在 drain loop `finally`、`loop.close()` 之前補一行 `loop.run_until_complete(agen.aclose())`。驗收看 6a/6b 轉綠 + 6c 仍綠。**不要**因為「plan 說三路徑」就去硬造例外路徑的 aclose 特例 —— 那是 over-fix。

---

## 6. F-PC-7 邊界（WS2 cross-repo）

F-PC-7 在 `paas-agent-runtime` repo，**本 explainer 看不到該檔**；以下輸入由該 repo 的 agent 盤好（plan WS2 表 + leader brief），本報告只負責解說「為何這個邊界充分」。

### 測什麼

verifier `verify_token` 目前處於 **B transitional**（commit `18d9602`）：token **缺 `token_use` claim** 時**放行**。Phase D 要切 **A 嚴格** —— 缺 claim → 拋 `TokenUseError`。這條是**唯一的新紅**。

### 三面覆蓋（紅 + 繼承綠）

| 面向 | token 形態 | 期望 | 狀態 |
|------|-----------|------|------|
| **缺欄位拒絕**（新紅） | 無 `token_use` claim | 拒（`TokenUseError`） | RED（B 放行 → A 拒） |
| **正向放行**（繼承綠） | `token_use=ws` | 通過 | 已綠續綠（Phase C C-1） |
| **反向拒絕**（繼承綠） | `token_use=skill_bundle` 打 ws 通道 | 拒（cross-scope） | 已綠續綠（Phase C C-1，commit 1305b77/7daf1f4） |

### 為何「泛用未知值」留到 green 階段

bogus / 空字串等**未知 `token_use` 值**的 regression test **不在本批 red**。理由：**B 過渡期的 verifier 對「非 ws 的已知值」本來就已拒**（不是放行），所以針對未知值寫 test **今天就綠 → 不是 red、無法驅動 A 嚴格切換**。它屬「A 切換完成後、green 階段補的 regression lock」，不是現在的 drive-red。tester 不寫「今天已綠」的東西當紅 —— 與第 5 章 F-PC-6c 同一條 failure-mode honesty 原則。

### 為何這個邊界充分

A 嚴格的**唯一語義變化** = 「缺 claim 從放行改成拒」。正向（ws 通過）與已知反向（skill_bundle 拒）由繼承綠守住不被新嚴格度誤殺。前置條件「operator 全簽 `token_use`」Phase C 已 merged → 切 A 嚴格不會打死合法流量。對稱面在 Odoo 端 `agent_runtime_skill.py:105` 已 A 嚴格，本條是把 pod 端對齊。**故單一新紅 + 繼承綠 = 充分**。

---

## 7. frozen contract — 凍結 env 名 + demo template xmlid

Phase D 有兩組「tester 硬編、impl 必須逐字對上」的契約。錯一個字 → charts mount / 讀取點 / test 三方錯位，且 cross-repo（charts vs operator vs paas-agent-runtime）無法互相 import 驗證，只能靠**凍結名對齊**。

### 7.1 凍結 5 env 名（軟契約，plan §「軟契約 env 名」表）

| env 名 | 消費端 | 狀態 |
|--------|--------|------|
| `AGENT_RUNTIME_AUTH_SECRET_CURRENT` | `agent_runtime_skill.py:33`（skill-bundle token 驗） | 既有（Phase C） |
| `AGENT_RUNTIME_AUTH_SECRET_PREVIOUS` | `agent_runtime_skill.py:34` | 既有（Phase C） |
| `AGENT_RUNTIME_WEBHOOK_SECRET` | `agent_runtime_webhook.py:27`（webhook HMAC current） | 既有（Phase C，**裸名**） |
| **`AGENT_RUNTIME_WEBHOOK_SECRET_PREVIOUS`** | F-PC-2 webhook verify | **本 session 新增** |
| **`AGENT_RUNTIME_NAMESPACE_SECRET`** | F-PC-3 `agent_session` | **本 session 新增** |

三方硬編這 5 名：charts 測（`test_agent_runtime_env.sh:31-37` `REQUIRED_ENVS`）、hardening 測（`:62-64` `_ENV_WEBHOOK_*` / `_ENV_NAMESPACE_SECRET`）、impl 讀取點。impl 任一處拼錯 → test 紅或 charts mount 對不上讀取點（runtime 缺 env → F-PC-3 fail-closed 炸）。

> **刻意不對稱（reviewer 別「順手對齊」）**：webhook **current 維持裸名** `AGENT_RUNTIME_WEBHOOK_SECRET`（不改 Phase C 已 ship 讀取點），previous 才加 `_PREVIOUS`；auth 則是 `_CURRENT/_PREVIOUS` 對稱。這個不對稱是**換取向後相容的有意設計**，不是疏漏 —— 不要把 webhook current 改成 `_CURRENT`。
> **namespace secret 刻意單一、不 rotation**：輪替會讓同 project 算出不同 hash → namespace 漂移 → 既有 pod 變孤兒。故無 `_PREVIOUS`，缺即 fail-closed（flag-gated，未 spawn 不需 secret）。

### 7.2 凍結 demo template xmlid（SEED.1）

impl 的 seed record xmlid **必須**是 `woow_paas_platform.agent_template_demo` 且 **unpublished**（`state != 'published'`）。test 三重斷言（存在 / 型別 / 未發布）把這條鎖死。reviewer 驗 `src/data/agent_template_demo.xml` 的 xmlid 與 state 逐字對上。

---

## 8. HC-2 lineage — F-PA-14 是「守 invariant」不是「新攻擊面」

`[[constraint-input-trust-expansion]]`（HC-2）在本 epic 已多次出現（Phase C 的 token-authoritative 是**攻擊面**應用：不信 attacker query param）。**F-PA-14 不同** —— 它是一個 **bootstrap-path 修補**，HC-2 在這裡是「**修的時候不准破 invariant**」的守線角色。

- **紅**（`with_hint`）= 加一條窄路徑：**可信** sudo 呼叫者透過 `bootstrap_owner_partner_id` context **明示** owner-partner，讓 sudo path 也能 bootstrap 真人 owner。
- **guard**（`without_hint`）= 鎖 HC-2 invariant：**無 hint 的 SUPERUSER create 不准自動讓 `__system__` 成 owner**。

reviewer 必須確認 fix 是「**明示 hint** 才 bootstrap」，不是「**sudo 一律 auto-own**」。後者會讓任何 sudo create（含內部自動化）誤植 owner row = HC-2 「attacker/automation 控制的 input 不可推導 authority」的反例。guard 就是把這條反例擋在 merge 前。**owner 身分來自呼叫端明示傳入的 partner_id，不是從 sudo 上下文推導** —— 這是 HC-2 的精神延續。

---

## 9. 刻意不測（with reason）

以下**有意不在本 session 測**，全有明確理由（pilot-gate / scope / 工具限制），不是覆蓋漏洞：

- **實機 WS connect（F-PC-1 runtime 面）**：websockets≥14 真裝起來 connect kwarg（`extra_headers` vs `additional_headers`）相容 —— **無 pod 無法單測**，列 pilot-gate re-verify（plan §test scope / §pilot 前 re-verify）。本 session 只測 pin 的靜態上界。
- **spawn-or-attach 真起 pod 端到端 / 15-type envelope 真語義 / SLO metric**：需 live cluster + pod，全列 pilot-gate，runbook flip 前 CAQA/reviewer re-verify。
- **per-route authorization / RBAC**（F-PC-4 邊界）：F-PC-4 只測 **global gate**（middleware 在 routing 前 fail-closed）；單條 route 的細粒度授權**不在 scope**。
- **相鄰 project field 的 UI**（UI.1 邊界）：只加 `use_agent_runtime` toggle，**不順手**加 `external_mcp_allowed` 等相鄰 field（plan WS3 明示超 scope）。
- **實際 flip flag / bind pilot project**（WS3 邊界）：本 session 是 **code-ready**，UI/seed 備好但**不 flip、不 bind 真 project** —— 那是 user-run runbook。
- **operator side-effect（Helm/k8s 真動作）**（F-PC-4）：middleware 在 routing 前跑，503 探針不需 live backend。

---

## 10. 隱性假設 / fixture 條件

reviewer / 後手 agent 須被告知這批 test 依賴的 DB state / env / mock：

- **env 注入模型**（OQ#1 ruling b）：secret 以 K8s Secret env 注入。test 用 `os.environ` 設 + `addCleanup` 還原（`_env_guard`，`test_agent_pod_phase_d_hardening.py:70-87`）。impl 必須讀這些**凍結 env 名**（第 7 章），不是 ir.config_parameter。
- **forward-coupling（F-PC-3 fail-closed 的 fixture blast radius）**：F-PC-3 上線後，**任何** create `agent_session` 的 test 若缺 `AGENT_RUNTIME_NAMESPACE_SECRET`，就會被 fail-closed 連坐變紅。處理分兩類（impl green 跑全套後校正，原 §10 誤把兩類都寫成「已預先設」）：
  - **新 Phase D test → tester 已自帶**：F-PC-2（`test_agent_pod_phase_d_hardening.py` setUp:108）、F-PC-6（`test_agent_pod_phase_d_sse_aclose.py` setUp）在**自身** setUp **已預先**設好 namespace secret（前瞻相容設計）。
  - **既有 Phase C session-creating test → impl 需補設**：既有建 session 的是 **3 個 class**（非原寫的「任何 test」≈5），impl 在 green 階段於 **3 檔 setUp** 補設 `AGENT_RUNTIME_NAMESPACE_SECRET`（**純 fixture provisioning，未動任何 assertion**）。其中 `test_agent_pod_phase_c_client_webhook.py` **一檔就含 3 個 session 建立點**：WH receiver setUp（直接建）、CL2（經 `spawn_or_attach`→`_project_hash` **間接建**）、CR2（drift cron）。`test_agent_pod_phase_a_acl` / `test_agent_pod_phase_c_skill_bundle` **不建 session → 未動**。
  - **reviewer 定性**：這是 F-PC-3 fail-closed 的**授權 blast radius（fixture-only、impl-added）**，屬 **forward-coupling**，**不是 cell-map drift** —— **test surface 不變（14 RED + 6 guard 不動）**，與第 2/3 章分類一致。
- **csrf token 取得**（F-PC-6）：從 `/woow` 或 `/web` 的 rendered bootstrap 解析 `odoo.csrf_token`；取不到會 `self.fail(...)` 明說是 fixture 問題、非 aclose red。
- **fake websockets module**（F-PC-6）：用 `sys.modules['websockets']` 替身 + `get_agent_runtime_client` 被 patch 成 fake（回 pod_ws_url + auth_token）→ route 進 bridge 而**不需 live operator/pod**。`_assert_reached_bridge` 先排除 8 個 setup-failure error_code，確保紅指 aclose、非沒進到 bridge。
- **admin（group_system）驅動 route**（F-PC-6）：project 用 `message_subscribe` 把 admin 設 follower 使 `_check_project_access` 過、channel 含 admin member、task 綁 channel→project。
- **project_access 安裝 gate**（F-PA-14）：setUp 若 registry 無 `woow_paas_platform.project_access` 會 `skipTest` —— 實際 run（Phase A 已 ship）會跑；reviewer 在缺該 model 的環境看到 SKIP 屬正常。
- **F-PC-4 blast-radius（D-1 已接受）**：fix 同 PR 須改 ~6 支既有 operator fixture（`test_api.py` / `test_tunnels.py` / `test_scale.py` / `test_agent_runtimes.py` / `test_odoo_module.py` 的 `api_key=""` + integration `conftest.py:102 app_client`）設真 key 或斷言 503。**這些 fixture 改動是 D-1 定案要求，不是 scope creep**（見第 11 章）。

---

## 11. 對 reviewer 的 PR diff 導讀

impl（green）PR 進來時，對照本報告這樣讀：

1. **先跑全 5 runner**（第 4 章），特別**手動補跑 host 的 F-PC-1 + charts** —— in-container CI 不會碰這 2 條，只看 Odoo suite 會誤以為「全綠」。
2. **RED 列全轉綠、guard 列仍綠**（第 2 章）。guard 變紅 = regression，優先級高於 RED 未轉綠。
3. **F-PC-6**：只該看到 drain loop `finally` 補一行 `agen.aclose()`；6a/6b 綠、6c 仍綠。**若 impl 對例外路徑加特例 = over-fix**（第 5 章）。
4. **F-PC-4 的 ~6 fixture 改動**：對照第 10 章 —— 這是 D-1 定案的 blast-radius，**不是越權改測**。operator 其餘改動應 router-additive（plan scope boundary）。
5. **凍結 env 名 / xmlid**（第 7 章）：impl 讀取點、charts mount、test 三方逐字對上；webhook current **保持裸名**別「對齊」成 `_CURRENT`。
6. **F-PA-14**（第 8 章）：確認是「明示 hint 才 bootstrap」，不是「sudo 一律 auto-own」。
7. **F-PC-7**（cross-repo，第 6 章）：A 嚴格切換需先確認 operator prod 全簽 `token_use`（deploy 順序見 runbook）；本 repo PR 不含該 repo 改動。

---

## 12. 數字校正（method count vs commit / brief）

延續 Phase A/C 慣例，透明對齊三個口徑的數字差異（這是 test-explainer 的可辯護性，不靠湊數字）：

| 口徑 | 數字 | 指的是 | 校正 |
|------|------|--------|------|
| brief / plan | **11 條 red** | caveat **條件**數 | F-PC-1/2/3/4/5/6/7 + F-PA-14 + UI.1 + SEED.1 + charts = 11 ✓ |
| commit title | **10 caveat guards** | **in-repo** 條件數（不含 WS2） | 11 − F-PC-7（cross-repo）= 10 ✓ |
| commit verified | **Odoo 10 fail of 710** | Odoo-side **RED 方法**數 | F-PC-2(1)+F-PC-3(2)+F-PC-5(2)+F-PA-14(1)+F-PC-6(2)+UI.1(1)+SEED.1(1) = 10 ✓ |
| commit verified | operator 1 / charts 1 / host 1 | 各 runner RED 方法 | F-PC-4(1) / charts(1) / F-PC-1(1) ✓ |
| **本報告（方法級）** | **14 RED + 6 guard** | test **方法**數 | in-repo RED 13（Odoo 10+operator 1+charts 1+host 1）+ WS2 1 = **14 RED**；in-repo guard 6（F-PC-2×2 + F-PC-4×2 + F-PA-14×1 + F-PC-6×1）|

> **無假紅**：F-PC-6 例外路徑（6c）與 F-PC-7 泛用未知值都**今天已綠**，tester 正確地**不**寫成紅（第 5、6 章）。commit「regression-guards green」列了 4 條 Odoo-side（F-PC-2 current/unknown、F-PA-14 no-hint、F-PC-6 exception）；本報告補上 commit 摘要未列的 operator-side 2 條（F-PC-4 configured-401、health-ok）→ in-repo guard 共 **6** 條。

---

## 13. Hand-off / sign-off

- **Gate 答覆**（「為何測這些、為何不測那些」）：見第 1 章 Gate 段 + 第 9 章刻意不測。每條 RED 有明確 production 失效模式；每條不測有 pilot-gate / scope / 工具限制理由。**閘門通過**，不退回 tester。
- **狀態**：`ready-for-implementation`（green phase）。impl 後驗收 = 14 RED 轉綠 + 6 guard 仍綠 + host 補跑 F-PC-1/charts。
- **未決（不阻塞 impl）**：design §2.15（namespace secret）待 follow-up 改寫（D-2 已定，plan 不改 design，另開條目）—— 屬文件對齊，非 code 缺口。
- **next**：回報 leader，leader 進 implementation（green）。implementation 若變更 test surface 與本報告 cell-map 不一致 → 退回本 explainer 補 report。
