# dev-sandbox dep hostPath 失效（extraAddonsHostPath rot）

- 日期：2026-05-17 / 2026-05-18
- 影響環境：local-mode dev sandbox（`sandbox.mode = "local"`，hostPath mount worktree src/ + dep addons）
- 涉及 chart：`charts/odoo-dev-sandbox`
- 受影響 sandbox（本 session）：`fix-sh-end-date-fresh-non-trial`（namespace `dev-sandbox-fix-sh-end-date-fresh-non-trial`，context `docker-desktop`）
- 修復方式：Path A — `helm upgrade --reuse-values --set` patch `codeMount.extraAddonsHostPath` + `kubectl rollout restart` + `odoo -u`（不是 SQL `UPDATE`）
- 預防：Path B 推薦 (b) — `create.sh` 把 `extraAddonsHostPath` 預設改成 `paas-odoo-ci/addons`，並加 fail-fast 檢查（見 §5）

## 1. 原由

`charts/odoo-dev-sandbox` 提供三種 mode：

| mode | 程式碼來源 | addons path | 適用情境 |
|------|----------|-------------|---------|
| `local` | hostPath mount worktree src/ + dep addons | `/mnt/extra-addons,/mnt/extra-addons-ext` | 本機 K3s / docker-desktop 開發（即時 reload worktree src/） |
| `ci-local` | CI image，addons 烤進 image | 同 local | CI 跑測試 |
| `stg` | Bitnami image，addons 烤進 `/opt/bitnami/odoo/custom-addons/` | bitnami flat | staging |

Local mode 有 **兩個** hostPath value 各管不同路徑：

| Value key | Mount path | 內容 | 來源（典型） |
|-----------|------------|------|------|
| `codeMount.hostPath` | `/mnt/extra-addons/{moduleName}`（= `/mnt/extra-addons/woow_paas_platform`） | 本 module 自己的 `src/`（live link，要 hot-reload 的對象） | 當前 worktree 的 `src/` 絕對路徑 |
| `codeMount.extraAddonsHostPath` | `/mnt/extra-addons-ext` | 8 個 dep addons（`ai_base_gt`、`sh_subscription`、`sh_subscription_wallet`、`payment_ecpay`、`payment_ecpay_ecpg`、`ecpay_invoice_tw`、`ecpay_invoice_website`、`odoo_ai_assistant_chatgpt_connector`） | `create.sh` 預設 `${PROJECT_ROOT}/extra/extra-addons`（per-worktree） |

Helm template 把這兩個 hostPath 對應 mount 進 pod（見 `charts/odoo-dev-sandbox/templates/odoo-deployment.yaml:91-128`）。Odoo `addons_path = /mnt/extra-addons,/mnt/extra-addons-ext`，要能 load `woow_paas_platform` 必須**兩條 path 都有對的東西**。

## 2. 問題

### 現象

兩個獨立但相關的訊號：

1. **`ir_module_module.state = 'to upgrade'` 卡住** — Verified by:
   ```sql
   SELECT name, state FROM ir_module_module WHERE name='woow_paas_platform';
   -- → woow_paas_platform | to upgrade
   ```
   Sibling deps（`ai_base_gt`、`sh_subscription` 等）仍是 `installed`，但 `woow_paas_platform`
   在 load 期間試圖 upgrade、失敗、卡 mid-flight。

2. **「pod 內檔案內容跟 worktree 不同步」（誤判）** — Tester 看到 test 跑出來像在用舊
   code，第一時間以為是 `codeMount.hostPath` rotted。實際 verify 是 **byte-identical
   正確**：
   ```bash
   diff <(cat src/models/subscription_lifecycle_mixin.py) \
        <(kubectl --context docker-desktop exec -n <ns> deploy/<odoo> -- \
            cat /mnt/extra-addons/woow_paas_platform/models/subscription_lifecycle_mixin.py)
   # → empty (byte-identical)
   ```
   真正的問題是另一邊：`extraAddonsHostPath` 對應的 dep mount **目錄是空的**，
   `woow_paas_platform` 在 module init 時 import dep 失敗，導致 module loader 把它丟進
   `to upgrade` state，後續 test loader 拿到 partially-loaded module，行為看起來像
   stale code。

### 觀察與排除

| # | 觀察 | 推論 |
|---|------|------|
| 1 | `kubectl describe pod` 顯示兩個 hostPath volume 都 mount 成功（無 K8s 層 error） | mount 機制本身正常 |
| 2 | `kubectl exec ... ls /mnt/extra-addons-ext/` 回傳 **空目錄** | dep mount source 已是空 |
| 3 | `ls /Users/.../floors/woow_paas_platform--featdecouple-cs-projects/extra/extra-addons/` 報 `No such file or directory` | host 上的 source dir **已被刪除**（另一個 worktree 被 `git worktree remove` 後消失） |
| 4 | `kubectl exec ... ls /mnt/extra-addons/woow_paas_platform/` 內容跟當前 worktree 一致 | `codeMount.hostPath` 不是 rotted 的那一邊 |
| 5 | `helm get values <release>` 顯示 `extraAddonsHostPath` 指向已刪除的 sibling worktree | 直接命中 root cause |

### Root cause

`create.sh` 預設把 `extraAddonsHostPath` 設成 `${PROJECT_ROOT}/extra/extra-addons`，
而本 repo 的 `extra/extra-addons` 是「當前 worktree 內的副本」— **per-worktree**，
本 worktree 被建立時可能借另一個 worktree 上的 dep dir（因為當下還在），等 sibling
worktree 被 `git worktree remove` 之後，host 上的 source dir 就消失了。K8s hostPath
不會主動 detect — 它 silent expose 一個空目錄給 pod，Odoo 看到 `addons_path` 內
deps 都不見了，module init 失敗。

具體 trigger 路徑（觀察到的）：

- sandbox 建立時 `extraAddonsHostPath` 指到 sibling worktree 的 dep dir（暫時 OK）
- 之後 sibling worktree 被 `git worktree remove`，dep dir 消失
- 當前 sandbox 的 helm release values 沒 reconcile，pod 重啟 / mount refresh 後看到空目錄
- 若觸發 `odoo -u woow_paas_platform`，module 進 `to upgrade` 但無法完成

**重要區分**：`codeMount.hostPath`（本 module src/）在這個 session **沒有** rot —
那條 mount 對應到當前 worktree，跟著 worktree 走，不會被 sibling worktree 動作影響。
真正脆弱的是 `extraAddonsHostPath` 的 default 設計（per-worktree 而非 per-repo 的
stable source）。

## 3. Fix（Path A — ad-hoc patch）

實際命令（命令本身可作為下次同症狀的 cookbook）：

```bash
# 1) Repoint extraAddonsHostPath 到 stable dep source（paas-odoo-ci 是 CI 用的
#    build context，contains 8 dep addons，跟任何 worktree 解耦）
helm --kube-context docker-desktop upgrade \
  fix-sh-end-date-fresh-non-trial \
  charts/odoo-dev-sandbox \
  -n dev-sandbox-fix-sh-end-date-fresh-non-trial \
  --reuse-values \
  --set codeMount.extraAddonsHostPath=/Users/eugene/Documents/woow/woow-paas-repo/paas-odoo-ci/addons

# 2) Rollout restart Odoo deployment 重 mount
kubectl --context docker-desktop rollout restart \
  deploy/fix-sh-end-date-fresh-non-trial-odoo-dev-sa-odoo \
  -n dev-sandbox-fix-sh-end-date-fresh-non-trial

kubectl --context docker-desktop rollout status \
  deploy/fix-sh-end-date-fresh-non-trial-odoo-dev-sa-odoo \
  -n dev-sandbox-fix-sh-end-date-fresh-non-trial --timeout=240s

# 3) Verify deps 出現在 new pod
kubectl --context docker-desktop exec -n dev-sandbox-fix-sh-end-date-fresh-non-trial \
  deploy/fix-sh-end-date-fresh-non-trial-odoo-dev-sa-odoo -- \
  ls /mnt/extra-addons-ext/
# → ai_base_gt, ecpay_invoice_tw, ..., sh_subscription_wallet（8 dirs）

# 4) Drain "to upgrade" state — 用 `odoo -u` 跑完整 upgrade hooks，**不是** SQL UPDATE。
#    SQL UPDATE 會跳過 migration hooks 留下 schema/data 不一致風險；-u 讓 Odoo 自己
#    跑完 migration 後 state 自動轉成 installed。
kubectl --context docker-desktop exec -n dev-sandbox-fix-sh-end-date-fresh-non-trial \
  deploy/fix-sh-end-date-fresh-non-trial-odoo-dev-sa-odoo -- \
  odoo -c /etc/odoo/odoo.conf -d woow_fix_sh_end_date_fresh_non_trial \
       -u woow_paas_platform --stop-after-init
# → "Module woow_paas_platform loaded in 0.96s, 954 queries"
#   "Modules loaded."（無 "inconsistent states"）

# 5) Restart pod 取得乾淨 runtime（step 4 的 --stop-after-init 已退出 odoo 主進程）
kubectl --context docker-desktop rollout restart \
  deploy/fix-sh-end-date-fresh-non-trial-odoo-dev-sa-odoo \
  -n dev-sandbox-fix-sh-end-date-fresh-non-trial
```

Post-fix verification：

```sql
SELECT name, state FROM ir_module_module
WHERE name IN ('woow_paas_platform','sh_subscription','ai_base_gt',
               'payment_ecpay_ecpg','ecpay_invoice_tw','sh_subscription_wallet',
               'odoo_ai_assistant_chatgpt_connector','payment_ecpay');
-- → 全 8 row state = 'installed'
```

### ⚠ 不要用 SQL `UPDATE ir_module_module SET state='installed'`

直覺反應是 SQL 直接改 state，但這會**跳過 Odoo migration hooks**：

- module version bump 對應的 `migrations/<version>/...` script 不會跑
- XML data file 的 `noupdate=False` record 不會 re-import
- post-load registry 不會重建

正確做法是 `odoo -u`，讓 module loader 跑完整個 upgrade pipeline，state transition
是副作用。SQL 改 state 留下 schema/data 不一致風險，未來下個 `-u` 才會炸而難 trace。

## 4. 同 session 的副 fix — `sh_journal_id` Path A

Fresh sandbox 在 `post_init_hook`（`src/hooks.py:138`）跑時還沒有 sale journal，
`res_company.sh_journal_id` 落 NULL。`_create_and_confirm_subscription` 第一行 guard
拋 `UserError`，所有 PR #38 / #42 test 變 ERROR（非 FAIL）。

Fix：

```sql
UPDATE res_company SET sh_journal_id = 1 WHERE sh_journal_id IS NULL;
-- journal id=1 通常是 'Customer Invoices' type=sale，SELECT 確認後再 UPDATE
```

**這條 SQL 與 hostPath upgrade 無耦合**。`odoo -u`（upgrade）跑 `migrations/` script
與 re-import XML，但**不會** re-run `post_init_hook`（那是 `-i` install-only path）。
所以 step 4 的 `-u` 不會把 `sh_journal_id` 清回 NULL。

Verify：

```sql
SELECT id, name, sh_journal_id FROM res_company;
-- → 1 | My Company | 1（仍 set，未被清）
```

若未來看到 sandbox 同時需要 data fix + module upgrade 的場景：每次 upgrade 後重跑
這個 SELECT 確認；若清回 NULL，UPDATE 是 idempotent 可重 apply。

## 5. Why Path A 是當下選擇（而非 destroy + recreate sandbox）

- destroy sandbox 會掉 DB，要重 init module + 重建 fixture data；對 in-flight PR 測試
  迭代成本太高（每次重 init ~5-10 min）。
- Path A 的命令鏈 5 步，全部可手動 verify。
- 已知 trade-off：Path A 不解決 root cause（`create.sh` `extraAddonsHostPath` default
  指 per-worktree 容易消失）— 列入 Path B。

## 6. Path B（follow-up — 預防再發）

Sandbox agent 把 root cause 拆成「local-mode hostPath model 假設 one-worktree =
self-contained addon tree」這個架構錯誤。本 repo 6 個 dep addon 住在 worktree
外面，靠 `extraAddonsHostPath` 帶進來，這個假設一開始就跟事實不符。兩個正解：

### Option (a)：Bake deps into Odoo image（proper fix，較大）

建一個 thin layer over `odoo:18.0`，build 時 COPY 8 個 dep addon 到
`/mnt/extra-addons-ext/`。Chart 的 local mode 只 hostPath mount **本 module 的 src/**，
dep 完全在 image 裡。

優點：

- 零 per-host config，與 CI 行為一致
- worktree 切換 / 移除完全不影響
- `extraAddonsHostPath` 可廢棄

缺點：

- dep addon 改動要 rebuild image
- image 要 push 到 registry 給 developer pull（或每個 dev 機 build 一次）

適合時機：有 CI 自動發佈 `paas-odoo:dev` 後（例如 nightly），local mode 直接吃這個
image 就跟 CI mode 一樣，只剩 worktree src/ overlay。

### Option (b)：Standardize 預設 dep source 至 `paas-odoo-ci/addons`（最小 change，**推薦立即做**）

`paas-odoo-ci/addons` 是 CI image build 用的單一權威 dep 集合，不綁任何 worktree，
不會隨 worktree 移除消失。

改動：

- `create.sh` 把 `extraAddonsHostPath` 預設改成
  `/Users/<user>/Documents/woow/woow-paas-repo/paas-odoo-ci/addons`（或 chart
  `values-local.yaml` 直接 pin）
- 加 precondition check：`create.sh` 啟動前 verify
  `<extraAddonsHostPath>/ai_base_gt/__manifest__.py` 存在，否則 fail fast 並告知使用者
  clone `paas-odoo-ci`

優點：

- 一行 `create.sh` change，不需 rebuild image
- 跟 CI 用相同 source-of-truth

缺點：

- 要求 developer 同時 clone `paas-odoo-ci` repo
- 跨 dev 機環境的絕對路徑略不同（可改用 `${HOME}/...` 或 env var）

**建議**：先做 (b)（一行 + fail-fast），(a) 等 paas-odoo image 進入 nightly publish
cadence 後再做。**任一條路徑都要實作 chart-level fail-fast**：`create.sh` 啟動前
verify dep source 不是空目錄、不是缺 `ai_base_gt/__manifest__.py`，這樣 rot 會在
**create 時就被擋**，而非在 module-load 失敗 10 分鐘後才浮上來。

## 7. 後續監測

部署到 stg / 下個 sandbox 後留意：

- `kubectl rollout restart` 之後 `kubectl exec ... ls /mnt/extra-addons-ext/` 是否
  非空（8 dir）。
- `ir_module_module.state` 是否會自動再退回 `to upgrade`（若會，可能 upgrade 順序或
  module init flow 還有 race condition，需 deeper trace）。
- 若 Path B option (b) 實作前同樣症狀再出現，留 incident 連回此 doc 並把 helm
  release 的 `extraAddonsHostPath` 直接強制 patch 到 `paas-odoo-ci/addons`。

## See also

- `charts/odoo-dev-sandbox/values.yaml`（`codeMount.hostPath` / `extraAddonsHostPath` 定義）
- `charts/odoo-dev-sandbox/templates/odoo-deployment.yaml:91-128`（mount template 邏輯）
- `charts/odoo-dev-sandbox/values-local.yaml`（local mode override；目前不 pin dep path）
- `.claude/skills/k8s-dev-sandbox/SKILL.md` + `scripts/create.sh`（sandbox lifecycle；建議
  改 `extraAddonsHostPath` default 與加 fail-fast 在此）
- 既有相關 doc：`docs/tracking/issues/stg-sandbox-networkpolicy-cross-ns-block.md`（同 chart 不同 issue）
- 同 session 的 sh_journal_id fix：見 `/tmp/tester-red-39.md` §「Sandbox quirks」第 2 點 + 本 doc §4
- Pattern doc 對應：`docs/decisions/patterns/plan-driven-date-helper.md`（PR #42 的 code 修法；
  本 doc 是 PR #42 session 中 sandbox 層遇到的獨立議題，不是同一個 fix 的兩面）
