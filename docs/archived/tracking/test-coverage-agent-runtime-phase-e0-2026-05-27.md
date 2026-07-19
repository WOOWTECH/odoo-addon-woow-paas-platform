---
name: test-coverage-agent-runtime-phase-e0-2026-05-27
phase: red-explained
team: feature-agent
test_explainer: agent-runtime-pod-test-explainer
plan_ref: docs/plans/2026-Q2/agent-runtime-pod-plan.md  # §Phase E0
design_ref: docs/plans/2026-Q2/agent-runtime-pod-design.md  # §8
tester_commit: 76836b8
base_commit: b46f0af  # #74 merge into develop（base 非 stale，§0 自檢確認）
created: 2026-05-27
updated: 2026-05-27
scope: F-PILOT-4（registry pull）/ F-PILOT-8（LLM credential）— operator unit red only
related:
  - docs/archived/tracking/test-coverage-agent-runtime-phase-d-2026-05-24.md
  - docs/archived/tracking/test-coverage-agent-runtime-phase-c-2026-05-24.md
status: archived
archived-from: docs/tracking/test-reports/test-coverage-agent-runtime-phase-e0-2026-05-27.md
archived: 2026-07-19
archived-reason: "一次性測試報告，對應功能已上線"
---

# Test Coverage Report — Agent Runtime Pod Phase E0（人類可讀版）

> 這份報告**不是** plan/design 的逐條翻譯（plan §Phase E0、design §8 是 source-of-truth）。
> 它解釋：**「Phase E0 為何只測這 5 條 operator unit、其餘為何不在本輪、本輪驗收到哪、哪些待後續」**，餵給 reviewer / CAQA / stakeholder 對齊測試意圖與**邊界劃分**。
>
> **Phase E0 是 pilot 揭露的 production gap cycle** — pilot e2e（echo agent round-trip）跑通後，補「真正生產化 agent pod」的兩個前提：私有 image 拉取（**F-PILOT-4**）+ 真 LLM agent 金鑰（**F-PILOT-8**）。echo 用私有 `jcr-prod` image 但 pilot 靠 ephemeral workaround 撐住、echo 不呼叫 LLM 故 pilot 沒撞 → 這兩缺口在 pilot 之後才浮現。
>
> **本輪 = operator unit scope only（`extra/paas-operator/`）**。跨 repo / 整合 / blocked 的 sibling test 在 tester commit 內**明確標記但不實作**。本報告的重心是把這個**四向邊界**講清楚。
>
> **reviewer special attention**：
> 1. 第 3 章 **四向邊界劃分** — 為何這 5 條 operator unit 即足夠、為何 T4.2/T4.3/T4.4/T8.2/T8.4 必須落 paas-charts / cluster / paas-agent-runtime、為何 F-PILOT-8 完整 e2e 卡 F-PILOT-10。
> 2. 第 5 章 **2 個 tester 標的對齊點**：(a) 誰建 LLM Secret（method-agnostic 斷言 + leader 釐清 operator runtime 建 per-release Secret）(b) `model_config` pydantic 命名雷（impl 用 `llm_model_config`，本輪只鎖 `api_key`）。
> 3. 第 6 章 **驗收不假綠** — 本輪只證 schema-carries-key + operator-provisions-Secret 的 unit/contract 面；真 image 拉取、SA 不可讀 secret、不落明文 pod spec、pod fail-fast、真 LLM e2e **全不在本輪**，明標待後續。

---

## 1. TL;DR

| 項 | 值 |
|---|---|
| 緣由 | pilot（echo round-trip）後揭露 2 個生產化前提（design §8，使用者 2026-05-26 拍板 4 decision point） |
| 本輪 scope | **operator unit only**（`extra/paas-operator/tests/test_agent_runtimes.py`，+190 行 additive） |
| red 數 | **5 條**（F-PILOT-4: T4.1a/T4.1b · F-PILOT-8: T8.1a/T8.1b/T8.3） |
| 既有 suite | **112 passed 續綠**（additive，未動既有） |
| 失敗性質 | 全 **missing behaviour**（非 import/fixture error）— fixture 全既有（見第 7 章） |
| 跨 repo / 整合 / blocked | **明標不實作**：T4.2/T4.2b · T8.2 → paas-charts；T4.3/T4.4 → integration/cluster；T8.4 → paas-agent-runtime；F-PILOT-8 full e2e → **卡 F-PILOT-10** |

**兩個缺口（design §8.1 / §8.2）**：

- **F-PILOT-4（registry pull）**：operator 動態起 pod 走 `agent_runtimes.py:129 helm_service.install(create_namespace=True)` 建**裸 ns**，**繞過** marketplace 的 `create_namespace_with_quota`（後者會呼 `ensure_registry_secret`）。雙缺口 → 私有 image `ImagePullBackOff 401`（pilot 靠 ephemeral copy workaround 撐）。
- **F-PILOT-8（LLM credential）**：真 agent pod 跑 LangGraph 要呼 LLM 需金鑰；來源已存在（Odoo `ai.config.api_key`）但**注入 pod 路徑未設計**。operator 不直連 Odoo DB → key 須經 spawn request 帶上 + operator 建 per-release Secret。

**Gate 答覆（「為何測這些、為何不測那些」）**：
- **測這 5 條** = 兩缺口在 **operator 程序內、可用 mock(helm+k8s) 確定性驗證**的部分：cold-start 是否呼 `ensure_registry_secret` + values 是否 forward `imagePullSecrets`（F-PILOT-4 機制）；schema 是否帶 `api_key` + cold-start 是否建 per-release LLM Secret + 是否 per-tenant 隔離（F-PILOT-8 機制）。
- **不測的部分** = 需要 **helm render（chart 在別 repo）/ live cluster（真拉 image、exec 試讀 secret）/ 真 LLM image（F-PILOT-10）** 才能驗的，全列對應 repo / 整合 / blocked（第 3 章）。**理由不是省略，是這些斷言在 operator unit 層無法成立**。

---

## 2. 為何測這些 — 5 red → 機制 / invariant 對應

| # | test 方法（`test_agent_runtimes.py`）| F-PILOT | 驗的機制 | 對應 INV | 今天紅的原因 | design / plan ref |
|---|------|---------|---------|---------|------------|------------------|
| T4.1a | `TestPilotGapRegistrySecret::test_T41a_cold_start_calls_ensure_registry_secret` | 4 | cold-start 在 install 前呼 `helm_service.ensure_registry_secret(ns)` 一次（與 marketplace 對稱）| — | 現 cold-start 從不呼 → ns 內無 pull secret | design §8.1（`helm.py:649`）/ plan WS-4a · T4.1(a) |
| T4.1b | `TestPilotGapRegistrySecret::test_T41b_build_install_values_forwards_image_pull_secrets` | 4 | `_build_install_values` 回傳 `imagePullSecrets:[{name: settings.registry_secret_name}]` | — | 現 values 無 `imagePullSecrets` 欄 | design §8.1（`agent_runtimes.py:58-81`）/ plan T4.1(b) |
| T8.1a | `TestPilotGapLlmCredential::test_T81a_spawn_schema_carries_api_key` | 8 | `SpawnOrAttachRequest` schema 宣告 `api_key` field（Odoo client 帶上、operator 不反查 DB）| — | 現無此 field → pydantic 靜默 drop 任何 api_key | design §8.2 / plan WS-8a · T8.1(schema) |
| T8.1b | `TestPilotGapLlmCredential::test_T81b_cold_start_provisions_per_release_llm_secret` | 8 | cold-start 建 per-release Secret `agent-pa-{hash}-llm` 且帶該 api_key | F-PILOT-8 INV-1 機制前提 | 現既不收 api_key 也不建 Secret | design §8.2 / plan WS-8a · T8.1(provision) |
| T8.3 | `TestPilotGapLlmCredential::test_T83_llm_secret_is_per_tenant_isolated` | 8 | project A/B 各 spawn，A 的 Secret 只含 A 的 key、絕不含 B 的（反之亦然）| **F-PILOT-8 INV-2** per-tenant 隔離 | 現完全沒建 per-tenant Secret | design §8.2 / plan T8.3（unit form）|

> **explicit vs 機制前提**：T4.1a/T4.1b/T8.1a 是直接驗「機制存在」；T8.1b 是 INV-1（不落明文）的**機制前提**（先有 operator 建的 Secret，才談得上 chart 用 secretKeyRef 引用它 → 但「不落明文 pod spec」本身是 chart render 面 = T8.2，不在本輪，見第 4 章）；T8.3 是 INV-2 的 **unit form**（整合 form = 兩個真 namespace，屬 cluster 驗收）。

---

## 3. 邊界劃分（核心）— 四向 + blocked

tester commit 把 F-PILOT-4/8 的測試切成四向。reviewer 最常問「為何不一次測完」—— 答案是**每條斷言的「可成立層級」不同**：有些只需 operator 程序內 mock 即可確定性驗，有些**本質上**需要 helm render / live cluster / 真 LLM image，在 operator unit 層寫不出來（或只能寫成假綠）。

| 向 | 條目 | 落點 | 為何在這裡（為何不能在 operator unit）|
|----|------|------|----------------------------------------|
| **① operator unit（本輪 ✓）** | T4.1a · T4.1b · T8.1a · T8.1b · T8.3 | `extra/paas-operator/` pytest（mock helm+k8s）| 這些驗的是 **operator 程序內的決策**：呼不呼 `ensure_registry_secret`、values 帶不帶 `imagePullSecrets`、schema 收不收 `api_key`、cold-start 建不建 Secret、key 是否 per-tenant 不串。全可用 mock 確定性斷言，**不需真 cluster** |
| **② cross-repo · paas-charts** | T4.2（chart 接 imagePullSecrets）· **T4.2b**（pod SA **minimal RBAC**：render 出的 Role/ClusterRole 不含 `secrets` 的 get/list/watch）· T8.2（`LLM_API_KEY` env `valueFrom.secretKeyRef` + pod spec **不含明文 value**）| agent-runtime chart `helm template` 斷言（paas-charts repo）| 這些驗的是 **chart render 結果**。chart 在 **paas-charts repo**，operator 看不到也 import 不到；`helm template` 才能 render 出 deployment/Role YAML。operator unit 無法斷言「render 出的 SA RBAC / env 形態」 |
| **③ cross-repo · paas-agent-runtime** | T8.4（pod 啟動缺 `LLM_API_KEY` → LangGraph LLM 初始化 **fail-fast**，非 silent / 非延到首呼）| pod runtime unit（paas-agent-runtime image）| 這是 **pod 內 runtime 行為**，在 agent image 的程式碼，與 operator 無關 |
| **④ integration / cluster** | T4.3（真 spawn project pod、私有 jcr-prod image → Ready 不 `ImagePullBackOff`）· **T4.4**（exec 進 pod 或 `kubectl auth can-i get secret/paas-registry` → **403 / no**）· T8.3 整合 form（兩個真 ns）| `tests/integration/` real-k8s（需 cluster） | 真拉 image 要 live kubelet + 真 registry creds；「SA 不可讀 secret」要真 RBAC 評估（exec→Forbidden）。mock 不能證明 kubelet 真的拉得動、也不能證明 RBAC 真的擋得住 —— 在 unit 層寫只會是假綠 |
| **⑤ blocked · F-PILOT-10** | F-PILOT-8 **完整 e2e**（pod 真的呼叫 LLM 回話）| 待 F-PILOT-10 真 LLM image | echo image **永遠不呼叫 LLM**，無法端到端證明「金鑰真的讓 pod 跑起 LLM」。需 F-PILOT-10 提供真 LLM agent image 才能 e2e。本輪只證 **schema 帶 key + operator 建 Secret** 的 unit/contract 面 |

**一句話總結邊界**：本輪鎖死「**operator 端契約**」（收 key、建 Secret、forward pull secret、per-tenant 不串）；**「render 形態 / 真拉取 / 真擋讀 / 真跑 LLM」全部往 ② ③ ④ ⑤ 推**，因為它們在 operator unit 層**無法不假綠地驗證**。F-PILOT-4 與 F-PILOT-8 各自跨 repo **lock-step**（plan §依賴：兩者獨立可平行）。

---

## 4. 安全 invariant 對齊（plan §安全 INV 553-556）

leader 點名的「INV-1：SA 不可讀 secret / LLM key 不落明文」對應兩個 feature 各自的 INV。關鍵是**每條 INV 的可測形式落在哪一向**：

| INV | 內容 | 可測形式 | 落點 | 本輪？ |
|-----|------|---------|------|:----:|
| **F-PILOT-4 INV-1** | agent pod SA **無** `secrets` get/list/watch RBAC | chart render 斷言（**T4.2b**）+ 運行時 `kubectl auth can-i` / API 403（**T4.4**）| ② paas-charts + ④ cluster | ✗ |
| **F-PILOT-4 INV-2** | `paas-registry` dockerconfigjson 落 ns **僅供 kubelet**（imagePullSecrets），不 mount 為 volume / 不 project 為 env | chart 不得有引用該 secret 的 volume/env（render 斷言）| ② paas-charts | ✗ |
| **F-PILOT-8 INV-1** | LLM key **secretKeyRef、不落 pod spec 明文** | `helm template` render：env 為 `valueFrom.secretKeyRef`、pod spec 無明文 value（**T8.2**）| ② paas-charts | ✗ |
| **F-PILOT-8 INV-2** | **per-tenant 隔離**，跨租戶 pod 無對方 key | unit form（**T8.3**，本輪）+ 整合 form（兩真 ns，cluster）| ① operator unit ✓ + ④ cluster | **部分 ✓** |

> **本輪只直接守住 INV-2 的 unit form（T8.3）**。其餘 INV 的可測形式**本質上**是 render / cluster 面 → 不在 operator unit。reviewer 須知：**本輪綠 ≠ 安全 INV 全部驗證**，安全閉環要 ② + ④ 補齊（第 6 章明列）。

**一個關鍵安全非對稱（design §8.2，reviewer 易混淆）**：
- **registry creds**（F-PILOT-4）= 平台級、跨租戶 → pod **不該讀**（INV-1：SA 無 secret RBAC，只給 kubelet 拉 image）。
- **LLM key**（F-PILOT-8）= 該租戶自己的、per-tenant → pod **必須讀**（要呼 LLM）→ secretKeyRef 的價值不是「擋 pod 讀」，而是**不落 pod spec 明文**（`kubectl get pod -o yaml` 不可見），降 spec 洩漏面；且洩漏**不跨租戶**（per-tenant Secret）。

兩者都用 K8s Secret 但「pod 可不可讀」**相反** —— 這是有意設計，不是不一致。

---

## 5. 2 個 tester 標的對齊點

### 5.a 誰建 LLM Secret —— method-agnostic 斷言 + leader 釐清

**tester 在 class docstring 明確 flag 給 leader**（`TestPilotGapLlmCredential` docstring）：
> 這些斷言 **operator 建 Secret**（plan WS-8a / T8.1）。chart-side secretKeyRef + no-plaintext render 是 T8.2（paas-charts）。**若 impl 改成把 key 純當 helm value 餵給 chart 自己的 Secret template**（auth-secret value pattern），下面的 secret-provision seam 需 re-point —— 見 report。

**leader 釐清（本報告反映）**：LLM Secret 由 **operator runtime 建 per-release Secret**，不是部署期 existingSecret pre-create。理由：
- api_key 來自 Odoo `ai.config`、**per-project**；
- operator **runtime 經 spawn request 才知道**該租戶的 key（operator 不反查 Odoo DB）；
- 故**不可能**像 `AGENT_RUNTIME_AUTH_SECRET_*` 那樣在部署時用 existingSecret pre-create（那是固定的 operator 簽 key）；
- **「比照 auth-secret」只取 secretKeyRef 注入機制（不落明文），不取 existingSecret 的建立法**。

design §8.3 的慣例對照表正好佐證此區分：

| secret | 建立方式 | 消費 pod | pod 可讀？ |
|--------|---------|---------|:--------:|
| `AGENT_RUNTIME_AUTH_SECRET_*` | **existingSecret**（部署期，paas-cd pin）+ secretKeyRef | operator pod | — |
| `LLM_API_KEY`（F-PILOT-8 新）| **per-release Secret（operator cold-start 建）** + secretKeyRef | agent pod | ✓（必須） |

**斷言為何 method-agnostic（T8.1b / T8.3）**：tester 不綁定「用哪個 method 建 Secret」，而是掃**所有** mock 呼叫 —— `str(mock_k8s.mock_calls) + str(mock_helm.mock_calls)` 內找 `agent-pa-{hash}-llm` secret 名 + api_key（plain stringData **或** base64 data 皆接受）。**好處**：無論 impl 走「operator 直接 `k8s` create Secret」還是「operator 把 key 經 helm value 讓 chart Secret template 建」，只要 provisioning 在 helm/k8s 邊界**可觀察**，斷言即成立 → seam 改變不需改測。**但** leader 已 pin 預期 seam = operator runtime 建；若 impl 偏離（key 純當 plaintext helm value），「不落明文」的最終保證落在 **T8.2 chart render（②，cross-repo）**，不在本輪。

### 5.b model_config pydantic 命名雷

`SpawnOrAttachRequest` 若加「模型設定」欄位，**不可命名 `model_config`** —— 那是 **pydantic 保留屬性**（pydantic v2 用 `model_config` 存 model 設定），撞名會被 pydantic 吞掉 / 報錯。tester 在 T8.1a docstring 明寫：
> any model-config field must NOT be named `model_config`（pydantic-reserved）— use e.g. `llm_model_config`.

**本輪 schema red 只鎖 `api_key`**：T8.1a 只斷言 `"api_key" in SpawnOrAttachRequest.model_fields`，**不**斷言 model-config 欄位（避免把命名決策硬編進測試）。impl 加 model config 欄位時用 `llm_model_config`（或其他非保留名）即可，本測試不會擋。**reviewer**：看到 impl 用 `llm_model_config` 不是隨意命名，是規避 pydantic 保留字。

---

## 6. 驗收不假綠 —— 這輪到哪、哪些待後續

| 面向 | 本輪（operator unit）驗到 | 待後續（落點）|
|------|--------------------------|---------------|
| F-PILOT-4 pull secret 建立 | ✓ cold-start 呼 `ensure_registry_secret(ns)`（T4.1a）| — |
| F-PILOT-4 values forward | ✓ `_build_install_values` 帶 `imagePullSecrets`（T4.1b）| — |
| F-PILOT-4 chart 接線 + SA minimal RBAC | ✗ | **T4.2 / T4.2b** → paas-charts render |
| F-PILOT-4 真拉 image | ✗ | **T4.3** → integration/cluster（Ready 不 ImagePullBackOff）|
| F-PILOT-4 INV-1 pod 不可讀 secret | ✗ | **T4.4** → cluster（exec → 403 Forbidden）|
| F-PILOT-8 schema 帶 key | ✓ `api_key` in `model_fields`（T8.1a）| — |
| F-PILOT-8 operator 建 per-release Secret | ✓ `agent-pa-{hash}-llm` 帶 key（T8.1b）| — |
| F-PILOT-8 per-tenant 隔離 | ✓ unit form A/B 不串（T8.3）| 整合 form（兩真 ns）→ cluster |
| F-PILOT-8 INV-1 不落明文 pod spec | ✗ | **T8.2** → paas-charts render（secretKeyRef、無明文 value）|
| F-PILOT-8 pod fail-fast 缺 key | ✗ | **T8.4** → paas-agent-runtime image |
| F-PILOT-8 **真 LLM e2e（pod 真呼叫 LLM）** | ✗ | **卡 F-PILOT-10**（真 LLM image；echo 永不呼 LLM）|

> **明標**：本輪 operator unit 證的是 **schema-carries-key + operator-provisions-Secret + forwards-pull-secret + per-tenant-unit-isolation** 的契約面。**「真的拉得動 image / 真的擋得住 secret 讀 / 真的不落明文 / 真的 fail-fast / 真的跑起 LLM」一律待 ② ③ ④ ⑤**。任何人若據「本輪全綠」宣稱 F-PILOT-4/8 已生產就緒 = 誤讀 —— 安全與 e2e 閉環尚未驗。

---

## 7. 隱性假設 / fixture 條件

- **fixture 全既有 → red 是 missing behaviour 非 missing fixture**（已逐項核對）：`import base64`（`test_agent_runtimes.py:80`）、`_HASH`（:290）、`_NS`/`_RELEASE = "agent-pa-pa1234567890abcd"`（:291-292）、`_SPAWN_BODY`（:294）、`_names()`/`_build_install_values()`（Phase C 既有）、`settings.registry_secret_name = "paas-registry"`（`src/config.py:34`）全已定義。新 test in-function import 這些 → 紅落在斷言（行為缺），不落在 import/fixture。
- **secret 名推導**：`secret_name = f"{release}-llm"`，release 來自 `_names(_HASH)` → `agent-pa-pa1234567890abcd-llm`，對齊 design §8.2 的 `agent-pa-{hash}-llm`。
- **cold-start mock 套路**：`mock_helm.get.side_effect = HelmException(...not found...)` 逼 spawn 走 **cold-start**（install）分支；`mock_k8s.get_pods` 回 `Running/1`；`jwt_signer.sign_*` 回假 token → request 進到 install path 而**不需 live operator/helm/k8s**。
- **method-agnostic 檢查**（T8.1b/T8.3）：斷言掃 `str(mock_k8s.mock_calls)+str(mock_helm.mock_calls)`，key 接受 plain **或** base64（`base64.b64encode(...).decode()`）→ 不綁 Secret 建立的具體 API 形態（見 §5.a）。
- **既有 suite 不動**：commit additive（只 append 到檔尾 line 891 後），既有 **112 passed** 不受影響。
- **base 非 stale**（§0 自檢）：base `b46f0af`（#74 merge）是 `origin/develop` 祖先且為 e0 branch base；plan §Phase E0 + design §8 已在此 base 內。

---

## 8. 對 reviewer 的 PR diff 導讀（green phase）

impl（green）進來時對照本報告：

1. **本輪只看 operator（`extra/paas-operator/`）**：5 red 轉綠 + 既有 112 續綠。impl 改點預期 = `src/models/schemas.py`（加 `api_key` field）、`src/api/agent_runtimes.py`（cold-start 呼 `ensure_registry_secret` + `_build_install_values` forward imagePullSecrets + 建 per-release LLM Secret）、複用 `helm.py:649 ensure_registry_secret`（不重寫）。
2. **`api_key` schema 欄位**：確認非命名為 `model_config`（pydantic 保留，§5.b）。
3. **LLM Secret 建立 seam**：確認是 **operator runtime 建 per-release Secret**（§5.a leader pin），非把 key 當 plaintext helm value。
4. **不要把 ② ③ ④ ⑤ 的缺席當成覆蓋漏洞**（第 3/6 章）：本輪**刻意**不含 chart render / 真拉取 / exec-403 / pod fail-fast / 真 LLM e2e。這些是 paas-charts / cluster / paas-agent-runtime / F-PILOT-10 的責任，各自 lock-step。
5. **跨 repo lock-step 提醒**：operator 綠**不等於** F-PILOT-4/8 close —— paas-charts 的 WS-4b（imagePullSecrets 接線 + SA minimal RBAC）/ WS-8b（secretKeyRef + per-release Secret template）必須同步落地，安全 INV 才閉環。

---

## 9. Hand-off / sign-off

- **§0 啟動自檢**：✅ repo = woow_paas_platform（origin git-prod）/ branch = `feature/agent-pod-phase-e0` / base `b46f0af`（#74）非 stale，含 plan §Phase E0 + design §8 / tester red commit `76836b8` 只動 operator 測試（additive，112 續綠）。
- **Gate 答覆**（「為何測這些、為何不測那些」）：見第 1 章 Gate 段 + 第 3 章四向邊界 + 第 6 章驗收表。**每條測的有「operator 程序內可確定性驗」的理由；每條不測的有「本質需 render / cluster / 真 LLM image，unit 層會假綠」的理由。閘門通過，不退回 tester。**
- **狀態**：`ready-for-implementation`（green phase，operator unit scope）。
- **未決（不阻塞本輪 code）**：F-PILOT-8 產品策略（租戶自帶 / 平台代付 / quota / fallback / api_key 輪替 invalidate）—— design open item，plan 明示「不阻 code」。
- **next**：回報 leader → leader 進 implementation（green）。impl 若變更 operator test surface 與本報告對應不一致 → 退回本 explainer 補 report。
