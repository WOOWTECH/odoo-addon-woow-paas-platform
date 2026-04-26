# ECPay / 金流端對端測試報告

期間：2026-04-24 ～ 2026-04-26（四輪 + T19 實作）

最終 image：
- paas-odoo (stg): `paas-odoo-stg:20260426.1146`（含 T19）
- paas-odoo (prod): `paas-odoo:20260426.1204`（v18.0.1.0.13，含 T19）
- paas-operator (stg+prod): `paas-operator:20260426.0534`
- paas-operator chart: `0.1.8`

## 摘要

- 涵蓋 **27 個情境**：T01–T13（金流）、T17（cycle 連續扣款）、T18–T26（殘留事項 + 壓測）、A（BUG-5 HTTP）、T22 Phase B（real K8s）、T19 e2e
- 全部通過
- 找出並修復 **9 個 bug**（BUG-1 ~ BUG-9）
- 實作並部署 **1 個 feature**：T19 連續失敗自動取消（spec → code → unit + e2e test → stg + prod 部署）
- 新增 5 個 regression test 檔 + 1 個 operator integration test 套件
- 跨 4 個 repo 協作（paas-platform / paas-charts / paas-cd / paas-ci）

---

## 1. Log 標記覆蓋

| 標記 | 涵蓋情境 | 結果 |
|------|----------|------|
| `[BIND]` 全套 | T04 | ✅ |
| `[TOPUP]` 主流程 + 失敗邊界 | T01–T03 + V01 + 既有加值 | ✅ |
| `[TOPUP] cron cleanup` | T24（5/5 情境） | ✅ |
| `[PAY_TOKEN] _charge` | T01 + V01 | ✅ |
| `[PAY_TX] _set_done / _set_error` | T01 + V01 | ✅ |
| `[INV]` 全套 | T05, T06 + V02 | ✅ |
| `[SUB] cancel`（4 路徑） | T09 | ✅ |
| `[SUB_MIXIN]` create / skip_trial | T07 + T08 + T10 | ✅ |
| `[SUB_CRON]` 全套 | T11 + T22 | ✅ |
| `[SUB_FIX]` auto_invoice / handle_invoice | T12 + V03 + T17 + T26 + T19c | ✅ |
| `[AUTO_TOPUP]` 全套 | T12b + V03 | ✅ |
| `[LOW_BAL]` 全套 | T13 | ✅ |
| `[CLOUD_SVC]` deploy / change_plan / cleanup | T07, T10, V04 | ✅ |
| `[CLOUD_SVC]` _do_suspend / _do_resume | T22 (Phase A + B) | ✅ |
| `[SH]` 全套（create/suspend/resume/cleanup/change_plan） | T23 | ✅ |
| `[PLAN_CHG]` wizard | T25 | ✅ |
| 連續多週期扣款 | T17（2 cycle）+ T26（6 cycle） | ✅ |

---

## 2. Bug 列表（9 個）

### 🐛 BUG-1 — Topup 同步 ECPay reject 整筆 rollback

**症狀**：ECPay 同步回 `state='error'`，`api_topup` 寫 `topup.state='error'` 觸發 ValueError（Selection 沒此值），整個 request rollback → topup + tx INSERT 都消失，使用者看 500、後台無紀錄。

**修法**：只在 tx 還在 in flight (`draft/pending`) 時把 topup 推到 `pending`，終結態交給 `_set_done/_set_error` callback。

**驗證 V01**：之前 500 + DB 無紀錄 → 現在 200 `state=failed` + DB 有紀錄。

**Commit**：`3ec74fe` (paas-platform)

---

### 🐛 BUG-2 — `api_invoice_pay` response 的 `new_balance` 是扣款前舊值

**症狀**：`partner.wallet_balance` non-stored compute 走 `loyalty.card.points`。`_pay_from_wallet` 改 loyalty 後 recordset cache 還是舊值，response 顯示 paid + balance 沒變。

**修法**：`_pay_from_wallet` + `api_invoice_pay` 雙重 `partner.invalidate_recordset(['wallet_balance'])`。

**驗證 V02**：balance=100、付 30 → response `new_balance: 70.0`（之前 `100.0`）。

**Commit**：`3ec74fe` (paas-platform)

---

### 🐛 BUG-3 — Auto-topup 扣卡 + 加點成功，但 invoice 沒被付（最嚴重）

**症狀**：`_sh_create_auto_invoice` 餘額不足 → `_attempt_auto_topup` ECPay 扣卡 + `add_wallet_points` 加點 → 但下一行 `_handle_invoice_creation` 重讀 `wallet_balance` 還是 pre-credit cache → 走 `super()` fallback → invoice post 但 wallet 沒扣。結果：**卡被扣 + 多了 wallet topup + 一張 unpaid invoice**。

**修法**：`_attempt_auto_topup` 加完點立即 invalidate；`wallet_topup._on_payment_done` 也補一次。

**驗證**：
- V03 cron 跑完 invoice paid + balance=0.15
- T26 多輪驗證：6 cycle 連續扣款 balance 線性下降 573.85 × 6，drift 0.0000

**Commit**：`3ec74fe` (paas-platform)

---

### 🐛 BUG-4 — `cloud_service._do_cleanup` 參數順序顛倒

**症狀**：`uninstall_release(namespace, release_name)` namespace 在前；隔壁 `scale_release(release_name, namespace)` release 在前。caller swap 錯，paas-operator 收到 namespace=`svc-...` → reject `Namespace must start with 'paas-ws-stg-'`。Exception catch 但只 warning，state 變 `deleting`，K8s 資源全留下。

**修法**：caller swap args 為 `(namespace, release_name)`。

**驗證 V04**：args 正確進到 helm 階段。

**Commit**：`56143b2` (paas-platform)

---

### 🐛 BUG-5 — change_plan 在 trial 期間 refund > price

**症狀**：smart_home `change_plan` controller 公式 `price * (end - today) / (end - start)` 在 trial（today < start）時 `remaining > total` → refund 1.318 × price。Wizard 端同公式同 bug。

**修法**：抽 `sh.subscription.subscription._compute_unused_refund()` helper 加 `today < sh_start_date` 守衛回 0；controller + wizard 都改呼叫。新增 `tests/test_change_plan_refund.py` 7 個 case。

**驗證**：
- helper：trial→0、today==start→full price、mid→prorated、end→0、past→0、missing dates→0（6 case）
- wizard E2E：refund=0、wallet 不被 credit
- HTTP controller E2E（A）：`POST /api/.../change-plan` 回 `refund_amount: 0`
- 反向驗：paid mid-cycle (start-10d, end+20d, price=1000) → refund=666.67 = 1000×20/30 ✅

**Commits**：`ebc4063` fix + `0806c24` test (paas-platform)

---

### 🐛 BUG-6 — staging operator image 落後 source

**症狀**：T22 Phase B 跑 `_do_suspend` → operator 回 404 'Not Found'。看似 `/scale` endpoint 不存在。

**Root cause**：source code 在 `4a7bd1e` (2026-04-11) 已加入完整 `/scale` endpoint，但 staging operator image 是 `paas-operator:20260410.1508`（前一天），沒包進去。**不是介面缺失，是 deploy 流程沒同步**。

**修法**：`/woow:build-operator` 建 `operator-v20260426.0355` → CI build → kubectl set image 部署。

**Action item（已記錄）**：每次合 paas-operator 的 PR 後要一併跑 `/woow:build-operator` + 更新 staging deployment（或設 CI auto-deploy）。

---

### 🐛 BUG-7 — scale endpoint 不支援 StatefulSet

**症狀**：BUG-6 修完打 endpoint，Bitnami Redis 仍 404 'No deployments found'。實作只 `get_deployments(...)`，沒處理 StatefulSet。Bitnami Redis / PostgreSQL / Odoo charts 都用 StatefulSet → 整個 prod 用 Bitnami chart 的訂閱 suspend 都失敗。

**修法**：`extra/paas-operator/`：
- `services/helm.py` 加 `get_statefulsets` + `scale_statefulset`
- `api/releases.py::scale_release` 同時 query 兩種、scale 兩邊；neither exists 才 404
- `tests/test_scale.py` 重寫 6 個 case（deployment-only、sts-only、mixed、empty、kubectl-fail）

**驗證**：staging mixed Deployment+StatefulSet release POST scale?replicas=0 → `Scaled 2 workload(s) to 0 replicas`，K8s 兩邊都 scale；replicas=1 也 work。

**Commit**：`03e39e3` (paas-platform `extra/paas-operator/`) + image `20260426.0534`

---

### 🐛 BUG-8 — scale_release client/server contract mismatch

**症狀**：T22 Phase B 看 `_do_suspend` + `_do_resume` log 顯示**連續兩次** `kubectl scale --replicas=0`，第二次本來該 `--replicas=1`。Odoo client 用 `data={'replicas': N}` 送 body，operator 用 `Query(default=0)` 讀 URL 參數 → 永遠 0。`_do_resume` 看似 success 但實際 scale 0。

**修法**：`paas_operator.py::_request` 加 `params=` 支援，`scale_release` 改用 `params={'replicas': replicas}`。和 operator 既有 `tests/test_scale.py` 用 query string 一致。

**驗證**：log 顯示 `replicas=0` 然後 `replicas=1` ✅

**Commit**：`f705b3b` (paas-platform)

---

### 🐛 BUG-9 — paas-operator chart 缺 deployments/scale RBAC

**症狀**：BUG-7 修完打 endpoint，operator log `Forbidden: cannot patch resource "deployments/scale"`。ClusterRole 給 `deployments [get list watch create update patch delete]` 但沒給 `deployments/scale` 子資源。`kubectl scale` 走的是子資源。

**修法**：`woow-paas-charts` 的 `charts/paas-operator/templates/clusterrole.yaml` 加：
```yaml
- apiGroups: ["apps"]
  resources: ["deployments/scale", "statefulsets/scale"]
  verbs: ["get", "patch", "update"]
```
chart bump 0.1.7 → 0.1.8。

**Commits**：
- `cd72e7d` (woow-paas-charts) — chart RBAC + bump
- `71e3f1d` (paas-app-bundle-cd) — fleet bundle prod + stg bump 0.1.8
- `3014ae5` + `8e0028f` (paas-app-bundle-cd) — stg / prod values image.tag → 20260426.0534

**驗證**：staging 移除暫時 `paas-operator-stg-scale-patch` ClusterRole 後，純 chart RBAC + new image 仍能 scale 0↔1。

---

## 3. T19 — 連續失敗自動取消（feature 實作）

從 spec → 4 個 design decisions → 實作 → unit + e2e test → stg + prod 部署，全鏈完成。

### 4 個設計決定

| # | 決定 |
|---|------|
| 1 | Cycle 加守衛：失敗不推進 `sh_date_of_next_payment`、reuse 既有 unpaid invoice、cron filter `<= today` |
| 2 | 新 `mail_template_subscription_cancelled` + `woow_cancellation_notified` dedup 欄位 |
| 3 | `threshold=0` = disabled（不是 infinite）|
| 4 | 硬性取消：threshold 預設 6（≈ 6 天 daily retry），不允許 in-place late recovery；要回服務走 re-subscribe |

### Code 交付

| 項目 | LOC | 位置 |
|------|-----|------|
| 3 個 model 欄位（counter / failure_at / notified flag）| ~35 | `models/sh_subscription_warning_field.py` |
| Cron 重構（拆 helpers + dunning state machine）| +324 | `models/sh_subscription_fix.py` |
| Cancellation email template + helper | +63 | `data/mail_templates.xml` + 同檔 |
| Migration 18.0.1.0.13 backfill | ~35 | `migrations/18.0.1.0.13/post-migrate.py` |
| Unit tests T19a-g | ~200 | `tests/test_subscription_failure_handling.py` |
| **Total** | **~700** | |

**Commit**：`217ba17` (paas-platform，tag `v18.0.1.0.13`)

### Test 結果

| 階段 | Tests | 結果 |
|------|-------|------|
| Unit (本機) | 7 (T19a-g) | ✅ |
| Unit (stg hot-patch) | 18 (T19 + LowBalance) | ✅ |
| Unit (stg baked image) | 7 | ✅ |
| **E2E full flow (stg)** | T19c real wallet+token+cron+email+lifecycle | ✅ |

E2E 驗 4 個 invariants：
- invoice reuse（3 cycle 共 1 張，不是 3 張）
- threshold 觸發 `state=cancel`
- email 真渲染（4780 chars body 含 plan name / 6 months 期 / failures count）
- `subscription_lifecycle_cron` 接手 → `cloud_service.state` running → suspended

### Spec 文件

`docs/spec/subscription-failure-handling-gap.md`

---

## 4. 測試結果

### 4.1 第一輪：T01–T13、T17（金流主流程 + 2 cycle）

詳見 BUG-1/2/3/4 的 V01–V04 驗證 + T17 連續 2 cycle 都過。

### 4.2 第二輪殘留事項：T18–T25

| # | 任務 | 結果 | 重點 |
|---|------|------|------|
| T18 | trial → cron 自然首次扣款 | ✅ | trial_end=昨日、next=今日 → cron 出 invoice paid + next +1月 |
| T19 | 連續 N 次失敗自動取消 | ✅ 後續實作（見上節） | 當時：未實作，寫 spec doc |
| T20 | 不同 sh_unit (year/week/day) | ✅ | 各 unit 用 relativedelta 推進正確；cron 與 confirm 兩條路徑都驗 |
| T21 | sh_recurrency > 1 | ✅ | rec=3 month → next +3 月 |
| T22 | `[CLOUD_SVC] _do_suspend / _do_resume` Phase A | ✅ | state machine + log（fake helm release）|
| T23 | `[SH]` smart_home 全套 | ✅ | create/suspend/resume/cleanup/change_plan 全觸發 |
| T24 | `[TOPUP]` cron cleanup | ✅ 5/5 | A) no_tx→cancelled  B) tx done→catch-up +50  C) tx error→failed  D) >60min→hard-cancel  E) <30min→不動 |
| T25 | `[PLAN_CHG]` wizard | ✅ | wizard.action_change_plan() 4 個 log 都觸發 |

### 4.3 第三輪：T22 Phase B（real K8s）+ T26 + A

#### T22 Phase B — real K8s scale_release E2E

走真實 marketplace 部署 → expire → cron → operator → kubectl scale。**過程連環踩到 BUG-6/7/8/9**（見 § 2 Bug 列表）。

最終驗證（不依賴暫時 patch）：
```
POST /api/releases/{ns}/final-test-rel/scale?replicas=0
→ HTTP 200 "Scaled 2 workload(s) to 0 replicas"
   deployments: final-test-app, statefulsets: final-test-db
→ K8s pods 全部移除 ✅
POST .../scale?replicas=1 → HTTP 200 → pods 全部回來 ✅
```

#### T26 — 6-cycle 訂閱壓測

```
✅ cycle 1: invoices 0→1 paid=1 balance 5000.00→4426.15 (Δ573.85) next +30d
✅ cycle 2: invoices 1→2 paid=2 balance 4426.15→3852.30 (Δ573.85) next +30d
✅ cycle 3: invoices 2→3 paid=3 balance 3852.30→3278.45 (Δ573.85) next +30d
✅ cycle 4: invoices 3→4 paid=4 balance 3278.45→2704.60 (Δ573.85) next +30d
✅ cycle 5: invoices 4→5 paid=5 balance 2704.60→2130.75 (Δ573.85) next +30d
✅ cycle 6: invoices 5→6 paid=6 balance 2130.75→1556.90 (Δ573.85) next +30d
```

Invariants：6 cycle exactly 1 invoice + paid 各一張、Δ 完全相同（無 drift）、Total 3443.10 = 6×573.85（精度 0.0000）、next +30d、BUG-3 cache 在多輪下穩定。

#### A — BUG-5 HTTP controller E2E

```bash
curl -X POST .../api/workspaces/17/smarthomes/9/change-plan \
  -d '{"jsonrpc":"2.0","method":"call","params":{"plan_id":10}}'
```
→ trial 期間 `refund_amount: 0`，wallet 不被 credit；DB 一致（舊 sub close、新 sub in_progress）
→ paid mid-cycle 反向驗 refund=666.67 = 1000×20/30 ✅

### 4.4 第四輪：T19 e2e

見 § 3「Test 結果」。

---

## 5. 部署狀態（最終）

| Bug / Feature | Stg | Prod |
|---|-----|------|
| BUG-1 / 2 / 3 / 4 | ✅ | ✅ baked (`paas-odoo:20260426.1204`) |
| BUG-5（change_plan trial refund）| ✅（含 HTTP path）| ✅ baked |
| BUG-6（operator image lag）| ✅ | ✅ |
| BUG-7（StatefulSet scale）| ✅ | ✅ |
| BUG-8（client query string）| ✅ | ✅ baked |
| BUG-9（chart RBAC）| ✅ | ✅ |
| **T19**（連續失敗自動取消）| ✅（含 e2e）| ✅ baked |

**Note**：prod paas-odoo image baked 但 module 尚未 install 到 prod `bitnami_odoo` DB（因為 prod 還無 active tenant）。下次 tenant onboarding 自動帶 v18.0.1.0.13。

### 部署工具對應

| 工具 / Tag | Image | 部署位置 | 用途 |
|-----------|-------|---------|------|
| `/woow:deploy-stg` | `paas-odoo-stg:{ts}` | `dev-sandbox-stg` namespace | **測試** sandbox |
| `git tag v18.0.1.0.x` | `paas-odoo:{ts}`（無 `-stg`）| paas-app-bundle-cd Fleet | **正式** prod |
| `/woow:build-operator` → `operator-v{ts}` | `paas-operator:{ts}` | Fleet bundle 控制 | 共用，prod/stg 各自 values |

---

## 6. 跨 agent / 跨 repo 變更鏈

| Repo | Agent | 變更 | Key Commits |
|------|-------|------|-------------|
| **woow_paas_platform** | paas-platform (本 agent) | BUG-1/2/3/4/5/6/8 + T19 + tests + skill | `217ba17` (T19), tag `v18.0.1.0.13` |
| **woow_paas_platform/extra/paas-operator** | paas-platform | BUG-7 fix（StatefulSet 支援）+ unit tests + integration tests | `03e39e3`, `fe8f821`，image `20260426.0534` |
| **woow-paas-charts** | paas-charts (woow-op 委派) | BUG-9 chart RBAC fix + bump 0.1.8 | `cd72e7d` |
| **paas-app-bundle-cd** | paas-cd (woow-op 委派) | fleet bundle bump 0.1.8 + stg/prod operator + prod paas-odoo image bumps | `71e3f1d`, `3014ae5`, `8e0028f`, `7bdae99` |

**跨 agent 觀察**：
- `paas-cd` agent 沒直接連線，全程透過 `maestri ask "woow-op"` 中介（woow-op 主責 woow-machines-operator，但「可協助」其他 repo）
- `maestri ask` 每步都有 approval prompt（cmux 的 per-command-pattern cache），多步任務建議一開始請對方 `shift+tab` 全 session 同意

---

## 7. Operator integration test（新安全網）

`extra/paas-operator/tests/integration/`（commit `fe8f821`）：

- 7 個 test 用 kind / 既有 K8s + httpx 走真實 endpoint
- 涵蓋：Deployment / StatefulSet / Mixed / 404 邊界 / BUG-8 contract pin（query string vs body）
- 預設 `pytest -m "not integration"` 不跑（不影響 dev）；本機 opt-in `pytest -m integration tests/integration/`
- 無 CI runner（須 reachable K8s cluster；docker-desktop / woow-k3s / kind 均可，見 `extra/paas-operator/tests/integration/README.md`）

**設計目的**：擋未來再有 BUG-7/8/9 這類 client/server contract drift 或 workload-type 缺漏。本機驗 9.68 秒、7/7 pass。

---

## 8. 教訓 / lessons learned

1. **對外部 service 的整合測試一定要至少一條真打 endpoint 的 path**。BUG-7/8/9 都是純 unit test 抓不到的（mock 雙方互相符合，但跟現實不符）。已開 operator integration test 套件。
2. **Deploy 流程要跟 source code 同步**（BUG-6）。每次合 paas-operator PR 要記得 build + bump fleet values；future work：CI auto-deploy。
3. **State machine 守衛要連 cron filter 一起改**（T19 Decision 1）。原 spec 的 `next_payment 不推進` 與 cron `==` filter 衝突，會讓 sub 卡死；必須一起改成 `<= today`。
4. **Cache invalidation 是 ORM 容易踩的坑**（BUG-2/3）。non-stored compute（`partner.wallet_balance` 走 loyalty card points）改完 underlying source 後 recordset cache 不會自動 refresh，要 `invalidate_recordset(['field'])`。
5. **參數順序統一**（BUG-4）。同一群 helper（`scale_release`、`uninstall_release`）參數順序不一致會踩。

---

## 9. Staging 終態

| 項目 | 狀態 |
|------|------|
| paas-odoo image | `paas-odoo-stg:20260426.1146`（含 T19 + 9 bugs）|
| paas-operator image | `paas-operator:20260426.0534`（含 BUG-7 fix）|
| paas-operator chart | `0.1.8`（含 BUG-9 RBAC fix）|
| K8s 殘留資源 | 無 |
| DB 資料 | 全 0：sub / invoice / service / smart_home / workspace / wallet_tx / loyalty points |
| 保留設定 | ECPay provider 啟用、ECPG 開、`web.base.url=https://stg.woowtech.io` (frozen)、`topup_payment_enabled=True`、paas-operator URL/key/prefix |
| 保留資料 | 1 張 active 卡片 token（`**** 2222`）|

---

## 10. 後續候選（剩餘 backlog）

| 項目 | 位置 | 優先 |
|------|------|------|
| Prod 觀察 T19 在實際 tenant 場景的行為 | prod 等首個 tenant onboarding | 中 |
| paas-operator PR merge 後自動 build + 更新 staging | CI workflow | 中 — 防 BUG-6 重現 |
| ~~處理 pre-existing test 失敗~~ ✅ done 2026-04-26（commit `11a812f`，見 § 11）| `tests/` | — |
| T19 後續：suspend 後 re-subscribe 重接既有 K8s 資源 | `models/sh_subscription_fix.py` + marketplace flow | 低 — 6 個月 cleanup window 內可手動處理 |

可繼續：T19 在 prod 等 tenant onboarding 自動生效；其他 backlog 項目可由下個 session 接手。

---

## 11. Pre-existing test failures cleanup（2026-04-26）

把第 10 節列為「中」優先的 backlog 一次清掉。實際範圍比 note 預期更廣：跑全套發現 **22 個 failure（11 failed + 11 errors）**，跨 4 個 test 檔，全部根因找到並修復。

最終：**256 tests, 0 failed, 0 errors**（commit `11a812f` on `develop`）

### 5 大根因類別

1. **Mock path 對應 import 位置**（5 ERROR）
   `cloud_service.py` 的 `get_paas_operator_client` 是 method 內 deferred import → patch `models.cloud_service.X` 在 module attribute 層找不到 → `AttributeError`。改 patch source `services.paas_operator.X`。
   ⚠️ smart_home 不能套同一規則——它 top-level import 把名字 bind 到自己 module namespace，patch source 不會影響到它，要 patch 自己 module-level。一開始 replace_all 沒區分，副作用打破 `test_smarthome_resume`，回頭分開處理才綠。
2. **Test runner 預設 admin user，bypass 邏輯吃掉 UserError**（2 FAIL）
   `_check_operation_allowed` 的 `base.group_system` early-return 把 `UserError` 蓋掉。setUp 加 `regular_user`，blocked-operation tests `with_user(regular_user)`。
3. **Impl 真實 bug**（1 FAIL → 順便修）
   `_is_past_grace_period` 短路順序錯：`if not sh_end_date: return False` 在 `if state == 'cancel': return True` 之前 → 沒 end_date 的 cancelled subs 被當成「未過 grace」。Cron 自己有獨立 cancel 分支不受影響，唯一行為改動在 `_check_operation_allowed` callers（更嚴格、更安全）。
4. **Method 搬家／signature 改變未同步測試**（10 ERROR + 3 FAIL）
   - `_build_mcp_sidecar_config` 從 `PaasController` 搬到 `cloud_service` model（paas.py:1446 留了註解，但測試沒同步）→ 6 ERROR。
   - `MAX_SYNC_RETRIES` 從 3 改成 10，2 個 cron 重試 tests 還用 3。
   - `_filter_allowed_helm_values` 改回 tuple `(filtered, rejected)`，4 個 helm filter tests 沒 unpack。
   - `tx.amount == -250.0` 數字 typo（invoice 80 → tx 應 -80）。
5. **測試 assertion 邏輯壞掉**（1 FAIL）
   `test_smarthome_suspend` 的三元表達式比對到 `None`，但實作回傳 `'http_status:503'` → 重寫成 `call_args.kwargs.get('service_url') == 'http_status:503'`。

### 修改檔案（5）

| 檔案 | 改動 |
|------|------|
| `src/models/subscription_lifecycle_mixin.py` | `_is_past_grace_period` 重排檢查順序 |
| `src/tests/test_subscription_lifecycle.py` | mock paths × 5 + regular_user fixture + smarthome assertion 重寫 |
| `src/tests/test_mcp_integration.py` | controller→cloud_service builder × 6 + retry_count 3→10 × 2 + invalid_json baseline 比對 |
| `src/tests/test_account_move_pay_from_wallet.py` | tx.amount typo |
| `src/tests/test_cloud_api.py` | tuple unpack × 4 |

### 教訓

1. **deferred import 的 mock path 要 target source**，但有 top-level import 的同名符號得分開處理（mock 是 namespace 操作，不是 "functional swap"）。
2. **Odoo TransactionCase 預設 admin uid=1**，所有走 `group_system` 短路的邏輯都要明確切非 admin user 才測得到。
3. **Method 搬家要把 references 全清完**——本案 `paas.py:1446` 留了註解但測試 6 個地方沒改，編譯/lint 抓不到。下次重構搬 method 直接 grep 全 repo `def \b{name}\b|\.{name}(` 全部對齊。
4. **`replace_all` 像 sed -i**：好用但會跨 context 誤傷，跨多個 patch path 要先確認語意一致再 batch。本案差點留下被打破的 `test_smarthome_resume` 當回禮。