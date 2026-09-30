---
title: "跨層 timeout 預算階梯 ＋ commit-on-secret 提交點（helm --atomic 的 2n 陷阱）"
status: accepted
date: 2026-07-30
deciders: [platform, report-leader-agent-team]
tags: [paas-operator, helm, timeout, async, invariant, repo-lint]
supersedes: []
related:
  - docs/decisions/patterns/helm-reset-values-unified-pipeline.md
  - docs/plans/2026-Q3/2026-07-30-461-reset-async-design.md
  - docs/tracking/test-reports/449-password-carrier-stg-e2e-2026-07-29.md
---

# 跨層 timeout 預算階梯 ＋ commit-on-secret 提交點

## Context

租戶按「Reset 密碼」的一次操作跨三層，每層各有一個 timeout，而**三個預算彼此不知道對方**：

| 層 | 改動前 | 問題 |
| --- | --- | --- |
| 前端 `jsonRpc` | 30s（共用預設） | 連**成功**案例都要 49.9s／76.7s ⇒ 瀏覽器先 abort |
| 平台 client | 120s（共用 `HELM_OPERATION_TIMEOUT`） | 與 operator 真正的上限無關，只是「helm 大概多久」的猜測 |
| operator | 最壞 ≈560s（helm 300 ＋ 2×`rollout status` 130） | 遠大於外層 ⇒ 外層必先斷線 |

每一層單看都合理，合起來卻**保證**冷 image cache 時逾時：`Recreate` ＋ RWO PVC 讓每次 reset 都要走「殺 pod → detach → 重排程 → attach →（可能跨節點）pull → 啟動」，落到哪個節點由排程器決定 ⇒ **逾時是機率事件**。#459 stg 實測：hermes-agent 與 open-design 第一次都逾時（120.6s／121.0s）、第二次都成功（49.9s／76.7s），唯一差別是 image 有沒有 cache（光 pull 954MB 就 1m9.963s）。

合成 chart 的 integration test（小 image、無 PVC、同節點）**永遠測不出來**，所以問題一路活到真機。

## Decision

### 1. 提交點左移：commit-on-secret

把「等 rollout 完成」整段移出 request。**提交點＝operator 回 2xx**：

- 2xx ⇒ 新密碼已是叢集權威載體（chart Secret／`config.json`）的值，且**不再有任何自動回退**。
- 2xx **之前**的任何失敗都保證叢集仍是舊密碼（`helm --atomic` 自己 rollback；config.json patch 失敗還原備份）⇒ 呼叫端「不落庫」永遠正確。
- rollout 成功與否不再影響密碼真相，只影響「服務何時恢復」——交給既有狀態機（stamp `upgrading` → 背景短 poll cron 每分鐘推進 → `running`），與 deploy／reconfigure 同形。#1159 起前端 5s 輪詢只讀 DB 的最近觀測，**不再**推動收斂（list/get 純讀）。

被取代的舊不變式是「DB 裡的密碼一定**生效過**」，它的成立條件是「operator 會在 rollout 失敗時把 Secret 換回去」。移除等待就等於移除那個 rollback，因此不變式必須改寫而不是沿用：**DB ≡ 叢集**在成功與失敗兩側都成立，代價是放棄「已生效」這層保證（rollout 仍可能失敗，但那時密碼記錄是對的、服務另有問題）。

殘留窄窗只有「2xx 在途遺失」（網路斷／程序被殺）：叢集已換、平台沒存。恢復手段是**再按一次 Reset**（reset 不需要舊密碼），不需要 pending 密碼欄位——那會為了一個窄窗把未確認的憑證落庫，並且要在 request 中途 commit（破壞 row lock 語意）。

### 2. 宣告一條可機械檢查的預算階梯

> **階梯不變式**：`前端 > 平台 client > operator 硬上限`，且 operator 硬上限由 operator 自己的常數算出來（單一權威）。

| 層 | 常數 | 值 |
| --- | --- | --- |
| 前端 | `RESET_REQUEST_TIMEOUT_MS`（`src/static/src/paas/services/cloud_service.js`） | 90_000 |
| 平台 client | `RESET_CONFIG_TIMEOUT`（`src/services/paas_operator.py`） | 75 |
| operator 硬上限 | `RESET_HARD_CAP_SECONDS`（`extra/paas-operator/src/api/releases.py`） | 65 |
| ├ helm subprocess | `RESET_HELM_SUBPROCESS_TIMEOUT_SECONDS` ＝ `2n + O` | 45 |
| ├ config.json exec | `RESET_EXEC_TIMEOUT_SECONDS` | 12 |
| ├ backup cleanup exec | `RESET_CLEANUP_EXEC_TIMEOUT_SECONDS` | 5 |
| └ 探索 ＋ restart 下達 | `RESET_KUBECTL_SLACK_SECONDS` | 3 |

外層天花板取 90s 而非更大，是為了繞開 Cloudflare 對 origin 回應約 100s 就回 524 的行為（**未實證**，但不必為此賭）。移除等待後典型端到端只花 5–15s，硬上限只在失敗路徑被逼近。

**守衛＝`scripts/audit/repo-lint.py` 的 `check_reset_budget_ladder`**（第 9 條 gate）：跨四個檔做小型常數折疊，驗兩層算式 ＋ 跨層不等式。散文註解攔不住「只改一層數字」——它已經被攔失敗過一次了。⚠️ gate 讀 `extra/paas-operator/**`，所以 `.gitea/workflows/repo-lint.yml` 的 `paths` **必須**含該路徑，否則對 operator-only PR 整條休眠。

### 3. `helm --atomic` 的 2n 陷阱（本 pattern 最容易被漏的一條）

`helm --timeout` 是 **per-operation**，不是 per-invocation。`--atomic` 失敗時 helm 會**自己再跑一次帶 wait 的 rollback**＝第二個 operation，再吃一次完整的 timeout：

```
成功路徑：  wait(≈0，Secret-only 不動 pod template) + O
失敗路徑：  wait(n) + rollback wait(n) + O        ⇒ 2n + O
```

若 subprocess 上限只給 `n + slack`，helm 會在 **rollback 進行中**被 SIGKILL：

- release 卡 `pending-rollback`
- Secret 停在**沒人持有**的新密碼（平台因為收到 500 而不落庫）
- ⇒ 正好打破 `--atomic` 存在的目的，也打破上面 commit-on-secret 的前提

**這不是新風險**：改動前 `helm.upgrade()` 根本不傳 `--timeout`（helm 用預設 5m）而 subprocess 上限是 `settings.helm_timeout=300s` ⇒ `2×300 > 300`，**atomic 失敗路徑本來就會在 rollback 開始前被 kill**。#459 沒撞到只因那兩次 upgrade 都成功。

真機量測（docker-desktop ＋ helm 3.13.3，合成 chart 強制 atomic 失敗）：

| 設定 | 失敗路徑牆鐘 | Secret | release status |
| --- | --- | --- | --- |
| subprocess `2n+O`＝45s | **30.4s**（＝2×15） | 回到舊密碼 ✅ | `deployed` ✅ |
| subprocess `n+10`＝25s（紅燈對照） | 25.1s（被 kill） | **停在新密碼** ❌ | **`pending-rollback`** ❌ |

`O`（非-wait 開銷：OCI pull ＋ render ＋ apply）必須**與 `--timeout` 分開記**，因為 `--timeout` 不約束 chart 下載與 render。本 repo 記在 `helm.py` 的 `HELM_CLI_OVERHEAD_SECONDS`（實測上界 ≈12s ⇒ 取 15）。

### 4. 「還同步 ⇒ 照舊補償；已非同步 ⇒ 不需補償」

補償邊界跟著提交點移動，兩者必須一起想：

| 子步 | 仍同步？ | 失敗處置 |
| --- | --- | --- |
| helm upgrade（`--atomic`） | ✅ | helm 自己 rollback ⇒ 叢集舊密碼 |
| Deployment 探索 / config.json patch | ✅ | 還原備份 ＋ helm rollback |
| `rollout restart` **下達** | ✅ | helm rollback ＋ 重新下達 restart（**不等**） |
| rollout **完成** | ❌ 已移出 | 不補償——沒有可補償的東西 |

補償路徑內的等待也要一併移除：否則一個失敗的 reset 要 2×130s 才回 500，補償反而衝破剛建立的天花板。

## 何時套用這條 pattern

1. **任何「HTTP 請求內等 Kubernetes 收斂」的端點**：先問「這個等待是提交所必需，還是只是想給呼叫端一個確認？」——後者一律移出 request，交給既有狀態機 ＋ 輪詢。
2. **動任何 `--atomic` / `--wait` 的 helm 呼叫點的 timeout**：先算 `2n + O`，再設 subprocess 上限。
3. **任何跨層預算**（前端／平台／operator，或任何 client/server 對）：與其挑「夠大的數字」，不如宣告階梯 ＋ 寫一條 gate。三個各自合理的數字合起來可以是必然失敗。
4. **改提交點**（把「等確認」改成「等承諾」）時：連同資料落庫的不變式一起重寫，並在程式碼註解裡寫下**舊語意為什麼不再適用**——否則下一個人會照舊語意「修正」回去。

## Consequences

- reset 從「唯一在 request 內等 rollout 的動作」回歸既有 upgrade 管線，少一條特例路徑。
- 順手修掉既有的 `2n > subprocess` 缺陷（非 reset 路徑仍有，記在 follow-up **F-461-3**）。
- 繼承既有管線的已知 fail-open：`_is_container_ready` 在 `pods` 為空時回 True（F-423-1）⇒ 極短窗口內 state 可能提前翻回 `running`。顯示層誤導，非資料損毀；`redeploy`／`reconfigure` 早已同樣暴露。
- `upgrading` 停滯沒有上限（F-461-1）。~~chart 版號漂移時「重按」不會收斂（F-461-2）~~ ✅ 2026-09-29 修：reset 改為釘住服務現在跑的 chart 版本（與 reconfigure 同一條 fallback），不再夾帶升版；15 秒預算只須容納「Secret 變、pod template 不變」的 upgrade。
- 測試上多了一條紀律：**「不等」與「等兩次」都只能在真叢集量到**。unit 測試 mock 掉 kubectl 後「有沒有等」只剩「有沒有呼叫某方法」，量不到牆鐘；`--atomic` 的 rollback 更是 helm 自己的行為，mock 裡不存在。故本 pattern 的兩條核心行為各有一支 integration test ＋ 各有紅燈對照。

## 術語

- **提交點（commit point）**：呼叫端可以開始把結果當事實寫進 DB 的那一刻。本 pattern 把它從「rollout 完成」左移到「Secret 已換且不會自動回退」。
- **預算階梯（budget ladder）**：外層 timeout 嚴格大於內層硬上限的巢狀關係；成立時「外層逾時」才重新變成異常訊號而非常態。
- **硬上限（hard cap）**：某層對自己最壞耗時的宣告，由該層自己的常數相加得出（單一權威，供外層與 gate 讀）。
- **2n 陷阱**：`--timeout` 是 per-operation，`--atomic` 失敗會多吃一次同額 wait（自動 rollback）。
