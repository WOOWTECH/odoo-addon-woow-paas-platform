# Runbook：agent-runtime pilot flip-gate（使用者自行執行）

> **狀態**：✅ **可交付執行**（gap-A/B/C 已拍 2026-05-25）。使用者自行執行（ops / 部署 + 2 週觀察）；本文件是**純 checklist**，整理時不代跑環境。
> **前置 code**：agent-runtime-pod epic Phase A–D 已 ship `develop`（#60 merged，全 flag-gated、inert）。flip / 部署 / 觀察由使用者依本文件自跑。
> **論述源（本文件只引用、不複製論述）**：
> - `docs/plans/2026-Q2/agent-runtime-pod-plan.md` §Pilot runbook / §Flip-gate caveats（**D-CAVEAT-1/2** 完整論述）/ §Flip gate 驗收
> - `docs/tracking/agent-runtime-pod-phase-d-enable-gate-2026-05-25.md` §5（剩給使用者的 flip-gate）
> - `docs/tracking/caqa-report-2026-05-25-agent-runtime-pod-phase-d.md` §Caveat（D-CAVEAT-1/2 機制詳述）

## 圖例

| 標記 | 意義 |
|------|------|
| 🔀 | merge / CI 動作（**不碰 cluster**）|
| ☸️ | 需 cluster + kubectl context（**務必 `--context <ctx>` 明確指定** — dev=`docker-desktop` / cross-env=`woow-k3s`；pilot 用 dev/stg sandbox context）|
| 🖥️ | Odoo admin 後台 UI 操作 |
| ✅ | 驗收點（可檢查訊號）|
| ↩️ | rollback |

> 指令中的 `<...>` 為**環境特定佔位符**（namespace / release / pod / secret 名、image tag、context），依你的 cluster 實況填入；本 runbook 不臆測這些值。

---

## 0. 部署順序硬約束（先讀）

```
🔀 F-PC-7 merge+build（A 嚴格 image）
        ↓
☸️ operator 就緒（簽 token_use，版本 ≥ Phase C）        ← 必先於 pod A 嚴格
        ↓
☸️ 建 K8s Secret(5 key 同值) → Odoo redeploy（吃 env）    ← secret 必先於 redeploy
        ↓
✅ secret 跨進程同值驗證（D-CAVEAT-1）
        ↓
🖥️ flip use_agent_runtime + bind pilot project + agent_template
        ↓
✅ spawn-or-attach 觸發 A 嚴格 pod（flip 後動態起）→ 端到端驗
        ↓
2 週 SLO 觀察
```

兩條**不可違反**的順序（code 無 gate 強制，靠本 runbook）：

1. **operator(簽 `token_use`) 先於 pod(A 嚴格)**（D-CAVEAT-2）— A 嚴格 pod 拒缺 `token_use` claim 的在途 token；operator 自 Phase C 已簽，但 `ws_token` exp 3600s → 若 operator 曾以 pre-token_use 版本簽過，最長有 **1h** 在途窗口被 A 嚴格 pod 誤殺。
2. **K8s Secret(5 key) 先於 Odoo redeploy** — Odoo 進程啟動才讀 env；secret 後補需 restart pod 才生效。

---

## 1. 🔀 F-PC-7 merge main + image build（A 嚴格）

**現況 [2026-05-26 更正 · stg 已完成]**：A 嚴格（`a8b3ce2` + `dabb275`）**已 merge main**（PR #3 `bb31676`，origin/main 已到 `3263352`/PR#4）+ build 進所有發佈 image（v1.0.0 / v1.0.1 皆 **unconditional A 嚴格、無 env toggle**）+ **stg pilot 已驗跑在 A 嚴格上**。本 step 對 **stg 已滿足**；保留作部署順序硬約束記錄（D-CAVEAT-2）+ **未來新環境（prod）部署仍適用**（見 §Prod 上線 gate）。〔原註：2026-05-25 reporter 在 stale clone 誤判 main 仍 B transitional `ba6b417`，B transitional 從沒 build 成 image → 已更正〕

- [ ] 在 `paas-agent-runtime` 開 **PR #3**：`feature/f-pc-7-token-use-strict` → `main`
- [ ] merge 後 CI build + push agent-runtime image（A 嚴格）到 registry（JCR）
- [ ] 記下 image tag（供 step 2 operator config 指向）

✅ **驗收**：image 在 registry；`runtime/transport/auth.py verify_token` 對**缺 `token_use` claim 的 token → reject**（A 嚴格，非 B 放行）。
↩️ **rollback**：本步純 merge/build，無 cluster 影響。後續若要退，operator 改指回 B transitional image（`ba6b417`）— **見 §Rollback 總表，必先退 pod image 再退 operator**。

---

## 2. ☸️ operator 就緒（簽 token_use）+ 版本確認

operator **code** 自 Phase C（`7daf1f4`，develop）已簽 `token_use`。但 ⚠ **部署的 operator image 未必是 Phase C+ 版本** —— 2026-05-25 查 `paas-app-bundle-cd`（Rancher Fleet GitOps）時，現役 stg operator image 是 `20260426.0534`（4/26，**Phase C merge 5/24 之前 → 不簽 `token_use`**）。**若不升級就往下 flip，A 嚴格 pod 會拒這個舊 operator 簽的無 `token_use` token → 連線炸。** 故 pilot flip 前**必先升級 operator image**。

pilot cluster 的 operator 須：(a) image 版本 ≥ 簽 `token_use`（Phase C+）、(b) spawn pod 用的 agent-runtime image 指向 step 1 的 A 嚴格 tag。

**operator 升級（現役舊版 → Phase C+）：**

- [ ] 確認 Phase C+ operator image 已 build：`build-paas-operator` CI 觸發於 `push develop` + `extra/paas-operator/**`，Phase C（`7daf1f4`）/ Phase D（`8d152f7`）merge develop 應已自動 build（tag = commit SHA 7 碼 + `latest`）。⚠ Gitea < 1.26 → `tea` 查不到 CI run，須 **Gitea web → Actions 確認 `build-paas-operator` 成功** + jcr `woow-paas-docker-local/paas-operator` 有對應 tag
- [ ] 更新 `paas-app-bundle-cd/paas-operator-stg/values-stg.yaml` 的 `image.tag`（`20260426.0534` → Phase C+ tag，建議 pin commit SHA 非 `latest`）→ `git commit + push`（Rancher Fleet 自動部署）
- [ ] 驗 stg operator 已 rollout 新版（operator pod 跑的 image tag = 升級後）：
  ```bash
  kubectl --context <stg-ctx> -n paas-stg-system get deploy -o wide | grep -i operator
  ```
- [ ] operator config（spawn pod 用的 agent-runtime image）指向 step 1 A 嚴格 image tag
- [ ] port-forward 驗 health：
  ```bash
  kubectl --context <ctx> port-forward -n <operator-ns> svc/paas-operator 8000:80
  curl -s http://localhost:8000/health
  ```

✅ **驗收（機器可驗 / gap-B）**：若 `/health` 含 `signs token_use: true` → 直接驗（CAQA/reviewer 建議的可檢查訊號）；**若無此欄位**（未必已實作，見 §Gap escalate）→ fallback **人工確認** operator 部署的 commit/version ≥ token_use 簽發版本。
↩️ **rollback**：operator 回退**前必先**把 pod verifier 退回 B transitional（D-CAVEAT-2 反向序）—— 否則 A 嚴格 pod 拒所有新 token。

---

## 3. ☸️ charts 部署（odoo-dev-sandbox 5 secret env）

`charts/odoo-dev-sandbox`（含 #60 的 env mount）。5 個 env 由**單一 K8s Secret**（`odoo-dev-sandbox.secretName` helper 算出的名，與 postgres 共用）的 5 個 key 注入，`secretKeyRef` 全 **`optional: true`**：

| 容器內 env | secret key | 同值要求 |
|------------|------------|----------|
| `AGENT_RUNTIME_AUTH_SECRET_CURRENT` | `agent-runtime-auth-secret-current` | **= operator 端 auth current** |
| `AGENT_RUNTIME_AUTH_SECRET_PREVIOUS` | `agent-runtime-auth-secret-previous` | **= operator 端 auth previous** |
| `AGENT_RUNTIME_WEBHOOK_SECRET` | `agent-runtime-webhook-secret` | **= operator 端 webhook current** |
| `AGENT_RUNTIME_WEBHOOK_SECRET_PREVIOUS` | `agent-runtime-webhook-secret-previous` | **= operator 端 webhook previous** |
| `AGENT_RUNTIME_NAMESPACE_SECRET` | `agent-runtime-namespace-secret` | Odoo 內部用；**不可輪替** |

- [ ] 建 / 更新該 K8s Secret，補上 5 個 key（auth/webhook 兩端同值；namespace 一旦設定**永不輪替** — 輪替 → 同 project 算出不同 hash → `paas-ws-pa-{hash}` 漂移 → 既有 pod 變孤兒）
- [ ] 部署 / 升級 odoo-dev-sandbox（用含 #60 的 chart）：
  ```bash
  ./.claude/skills/k8s-dev-sandbox/scripts/create.sh --context <ctx>    # 新建
  # 或既有 release：helm upgrade <release> charts/odoo-dev-sandbox --kube-context <ctx> ...
  ```
- [ ] **secret 建好後**才 restart / redeploy Odoo pod（讓進程啟動讀到 env）

✅ **驗收**：`helm template`（或 render 的 deployment）odoo 容器含 5 個 `AGENT_RUNTIME_*` env；Odoo pod restart 後 env 注入（值在 step 4 驗）。
↩️ **rollback**：移除 5 secret key + Odoo redeploy（env 變空）→ 因 `optional:true` 部署不炸；但 **flip 仍 on 時 namespace 空 → `agent_session` fail-closed**，故退 secret 前先 flip off（step 5 ↩️）。

> ⚠ **D-CAVEAT-1 放大點**：`optional:true` 使 **partial 部署（secret 只掛到部分進程）不在 deploy 時炸**，會靜默分歧到首次使用才現形（webhook 永遠 404 / hash 對不上）。故 **step 4 同值驗證必做**。

---

## 4. ✅ D-CAVEAT-1：secret 跨進程同值驗證

namespace / auth / webhook secret 須跨**所有消費進程**同值。code 無 startup 一致性斷言（gap-C），pilot 階段手動驗。

- [ ] **auth + webhook secret 兩端同值**（operator 簽 / Odoo 驗）— 比對 hash，不印明文：
  ```bash
  # Odoo 端
  kubectl --context <ctx> get secret <odoo-secret> -n <odoo-ns> \
    -o jsonpath='{.data.agent-runtime-auth-secret-current}' | base64 -d | sha256sum
  # operator 端對應 secret 同樣 sha256sum → 兩值必須相等
  ```
  （`agent-runtime-auth-secret-previous` / `agent-runtime-webhook-secret` / `...-previous` 各重複一次）
- [ ] **namespace secret 在 Odoo 內一致**：odoo-dev-sandbox 的 Odoo 為**單一 deployment**（web + cron 同 pod）→ 單 secret 天然一致；**若你的環境是多 replica / 獨立 cron worker**，確認都掛**同一個** secret。
- [ ] **確認 5 env 在 Odoo pod 內非空**（`optional:true` 缺值會是空字串 → namespace secret 空 → `agent_session._get_namespace_secret` `raise` fail-closed）：
  ```bash
  kubectl --context <ctx> exec -n <odoo-ns> <odoo-pod> -- printenv | grep AGENT_RUNTIME_
  ```

✅ **驗收**：auth/webhook 兩端 sha256 相等；namespace secret 非空且 Odoo 各進程同值。
↩️ **rollback**：值不一致 → 修正 secret + redeploy 再驗；**勿在不一致狀態下 flip**。

---

## 5. 🖥️ flip use_agent_runtime + bind pilot project + agent_template

- [ ] admin 後台選 **1 個 admin-controlled** pilot project（避免影響真實 user）
- [ ] bind agent_template（§4 model Phase A 已 ship；seed `agent_template_demo.xml` 為 **unpublished** 範本，pilot 須自建 / publish 一個真 template 並 bind）
- [ ] project form 設 **`use_agent_runtime = True`**

✅ **驗收**：`project.use_agent_runtime = True`；agent_template bound 且 published。
↩️ **rollback（一鍵，最快）**：`use_agent_runtime = False`（admin UI）→ pod 30min idle 自然 reap → 既有 `ai_client.py` 同步路徑接手，user 無感。

---

## 6. ✅ 端到端驗證（spawn-or-attach / WS relay / skill bundle）

flip 後 pilot project 第一次 chat invoke 觸發：

- [ ] **spawn-or-attach**：operator helm install 起 per-project pod（A 嚴格 image），namespace `paas-ws-pa-{hash}`
  ```bash
  kubectl --context <ctx> get ns | grep paas-ws-pa-
  ./.claude/skills/k8s-dev-sandbox/scripts/logs.sh <sandbox> --service odoo --follow   # 看 spawn / WS log
  ```
- [ ] **WS relay**（F-PC-1）：chat invoke → 收 SSE chunks（與既有 ai.assistant path 行為等效）；**實機驗 websockets connect 不 `TypeError`**（版本 pin `>=12.0,<14` 對）
- [ ] **skill bundle fetch**：pod 取 skill bundle 成功（`token_use=skill_bundle` 驗過；A 嚴格 pod 的 WS `token_use=ws` 驗過）
- [ ] **error 通道 + WS lifecycle**（F-PC-5 / F-PC-6）：人工注入一次 operator error（如暫斷）→ browser 收**真因非 cryptic**；對話結束**無 WS 洩漏**至 ping_timeout

✅ **驗收**：chat 端到端通；pod 起在正確 namespace；error surface 正確；無 WS 洩漏。
↩️ **rollback**：任一失敗 → step 5 一鍵 flip off + 收集 log → 退回 §Flip gate 驗收（plan）重查。

---

## 7. 2 週 SLO 觀察（gap-A 拍板：manual 近似量測）

> **[2026-05-25 拍板]** pilot 規模小，**用 manual log / `kubectl top` 近似量測**夠用；完整監控 stack（Prometheus / Grafana / alert rules）留 **Phase G**。

| SLO | 門檻 | manual 近似量測 | 動作 |
|-----|------|----------------|------|
| cold start p95 | **< 15s** | operator / Odoo log grep spawn-or-attach 起算 → pod ready / 首個 WS chunk 時間戳，抽樣算近似 p95 | 持續 > 15s → **暫停 phase advance** |
| WS error rate | **< 1%** | Odoo log grep SSE bridge error（`ai_assistant.py:721` `SSE-WS bridge failed`）+ operator error，對 invoke 次數比 | 超標查 error 通道 / pod 健康 |
| worker pool util | **< 50%** | `kubectl top pod`（Odoo pod CPU/mem）+ 數 active conv（每 active conv 佔 1 Odoo sync worker，design §6.8）對 worker pool size | 撞 **0.8** 告警 |

- [ ] 觀察 ≥ 2 週，三項 SLO（manual 近似）在門檻內
- [ ] 量測指令範例：
  ```bash
  kubectl --context <ctx> top pod -n <odoo-ns>                                                          # worker util
  ./.claude/skills/k8s-dev-sandbox/scripts/logs.sh <sandbox> --service odoo | grep -iE "SSE-WS bridge failed|spawn"   # error / cold start 抽樣
  ```

↩️ **rollback**：SLO 持續未達 → 一鍵 flip off（Layer 1），回報暫停 advance。

---

## Rollback 總表（兩層）

### Layer 1 — flip rollback（最快，pilot 出問題首選）

1. admin UI `use_agent_runtime = False`（一鍵）
2. agent pod 30min idle 自然 reap（或手動 `./.claude/skills/k8s-dev-sandbox/scripts/destroy.sh <sandbox>` / `kubectl --context <ctx> delete ns paas-ws-pa-<hash>`）
3. 既有 `ai_client.py` 同步路徑接手 → user 無感

### Layer 2 — deploy rollback（退 A 嚴格 / operator — **D-CAVEAT-2 反向序，順序不可錯**）

1. **先** flip off（Layer 1）
2. **先退 pod verifier → B transitional**（operator config 指回 B transitional image `ba6b417`）
3. **再退 operator**（若需降回 pre-token_use；否則保留簽 token_use 無害）
   - ⚠ **絕不可** operator 先降回 token_use baseline 以下、而 pod 仍 A 嚴格 → pod 拒所有新 token（D-CAVEAT-2 危害）
4. charts secret：移 5 key 因 `optional:true` 部署不炸，但**確認 flip 已 off**（否則 namespace secret 空 → fail-closed）

---

## Gap（已拍板 2026-05-25）

| gap | 結論 |
|-----|------|
| **gap-A 監控 stack** | **pilot 用 manual log / `kubectl top` 近似量測**（見 step 7），規模小夠用；完整監控 stack（Prometheus / Grafana / alert）留 **Phase G**。 |
| **gap-B operator `/health` `signs token_use`** | **列 follow-up**（Phase E+ fleet 規模補機器驗訊號，見 `docs/tracking/followups/follow-ups.md`）。**pilot fallback** = step 2 人工確認 operator 版本 ≥ token_use 簽發版本。 |
| **gap-C secret 同值 startup 斷言** | **列 follow-up**（Phase E+ 補 startup / health 一致性斷言，見 `docs/tracking/followups/follow-ups.md`）。**pilot fallback** = step 4 手動 `sha256sum` 兩端比對。 |

---

## Prod 上線 gate（未來式 · stg-pilot 之後才適用）

> ⚠️ **現在不適用**：本 epic 為 **stg-pilot-only**，無任何 prod agent namespace / prod agent pod。本節是**首個 prod agent pod 上線前**的前置 gate 記錄，屆時再執行；現階段不需動作。

**Gate（首個 prod agent pod 上線前必做）**：

- [ ] **確認 prod operator 全簽 `token_use`** — prod 環境部署的 operator image 版本 ≥ 簽 `token_use` 的版本（Phase C+，`7daf1f4` 後）。A 嚴格 pod **無條件**拒缺 `token_use` claim 的 token，**prod operator 若未全簽 → 首個 prod pod fail-closed、誤殺所有連線**。
  - 驗法同 stg step 2：operator `/health` 含 `signs token_use: true`（若 gap-B 機器訊號已實作）→ 直接驗；否則 **人工確認** operator 部署 commit/version ≥ `token_use` 簽發版本。
  - 部署順序同 D-CAVEAT-2：**operator(簽 `token_use`) 必先於 pod(A 嚴格)**；舊 operator 簽過的在途 `ws_token`（exp 3600s）最長有 **1h** 窗口被 A 嚴格 pod 誤殺。

🚩 **風險旗標（A 嚴格無安全網）**：

- A 嚴格 **無 runtime toggle**（unconditional 內建，無 env 開關可臨時降級放行）。
- **無 pre-A-strict image tag 可當 kill-switch**（B transitional 從沒 build 成發佈 image，回退無 B 版可退）。
- → 一旦 prod operator 全簽未就緒就起 pod，**誤殺只能靠「補簽」（升級/重啟 operator 重簽帶 `token_use` 的 token）或 hotfix**，**無法靠回退 image 救**。故 prod 上線**務必先驗 operator 全簽、再起首個 pod**。

> stg 對照：stg pilot 已跑在 A 嚴格 + operator-stg 已簽 `token_use`（empirical 驗過，見上 step 1/2 + `docs/tracking/followups/follow-ups.md` §F-PC-7）。prod 重複同一 gate 即可。

---

## Related

- Plan：`docs/plans/2026-Q2/agent-runtime-pod-plan.md` §Pilot runbook / §Flip-gate caveats（D-CAVEAT-1/2 論述源）/ §Flip gate 驗收
- Reporter durable：`docs/tracking/agent-runtime-pod-phase-d-enable-gate-2026-05-25.md` §5
- Review：`docs/tracking/caqa-report-2026-05-25-agent-runtime-pod-phase-d.md` / `docs/tracking/reviewer-report-2026-05-25-agent-runtime-pod-phase-d.md`
- Follow-ups：`docs/tracking/followups/follow-ups.md` §Phase D enablement gate
- Cross-repo：`paas-agent-runtime` F-PC-7 A 嚴格（`a8b3ce2`+`dabb275`）— **[2026-05-26 更正] 已 merge main（PR #3 `bb31676`，origin/main `3263352`/PR#4）+ build 進所有發佈 image，stg closed**；prod 全簽 gate 見上 §Prod 上線 gate
