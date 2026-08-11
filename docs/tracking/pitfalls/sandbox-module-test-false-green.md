---
name: sandbox-module-test-false-green
description: K8s dev sandbox 跑 woow-test.sh 印「✅ All tests passed」但實際 `0 failed, 0 error(s) of 0 tests`（false green）— 模組沒裝起來 + `-u` no-op + bare `--test-tags <module>` 不命中，三因疊加
metadata:
  type: pitfall
---

# Pitfall: K8s dev sandbox `woow-test.sh` false green — 0 tests 也報「✅ All tests passed」

> 來源：feat/project-setting 一次本機 sandbox 測試 session（2026-06-01）。`woow-test.sh` 報綠，但 Odoo log 實際是 `0 failed, 0 error(s) of 0 tests` —— **一個 test 都沒跑**。woow-test.sh 只看 `kubectl exec` 的 exit code（0）就印 banner，而 `odoo -u <uninstalled-module>` 對沒裝起來的模組是 no-op，exit 0、0 tests。改用 `-i woow_paas_platform --test-enable --test-tags /woow_paas_platform` 後實跑 **1142 tests**，false green 才破。
>
> woow-test.sh / CLAUDE.md 的修正另以小修分支處理（見本檔 §How to avoid 末段）。

## Symptom

```
✅ All tests passed for module: woow_paas_platform
```

但同一次 Odoo log 裡：

```
0 failed, 0 error(s) of 0 tests
```

- banner 綠，但 `of 0 tests` —— **沒有任何 test 被 collect / 執行**。
- `woow-test.sh` 的結果判斷只看 `TEST_EXIT_CODE -eq 0`（`kubectl exec` 的回傳），不檢查 log 裡的 `N tests` 計數，所以 0 tests 也算「passed」。
- 對照：模組真的裝起來、tag 對的時候，log 會是 `... of 1142 tests`（feat/project-setting 當時的實測數）。

## Why it happens

**主因是成因 1**（模組沒裝起來 → `-u` no-op，連 test 模組都沒被載入）；成因 2、3 是疊加/次要（會導致或維持「模組沒裝起來 / 0 命中」的狀態）。三者任一沒處理好都會退回 0 tests / false green：

### 1. 模組根本沒裝起來 → `odoo -u` 是 no-op

`create.sh` local 模式安裝時，odoo pod 開機會自動 init DB，與 create.sh 顯式下的 `-i "$MODULES"`（`base,web,woow_paas_platform` 起跳）**並發**，撞 DB unique constraint：

```
psycopg2.errors.UniqueViolation: ... pg_type_typname_nsp_index
```

安裝中止 → 模組停在 **uninstalled** 狀態，但 sandbox 仍報 created（create 流程沒因此 fail）。
之後 `woow-test.sh` 跑 `odoo -u woow_paas_platform`（`-u` = update **已安裝** 模組）對 uninstalled 模組是 **no-op**：不裝、不更新、不跑它的 test，exit 0、0 tests。

→ 首裝要用 `-i`（install），或先確認模組 `state=installed` 再用 `-u`。

### 2. 第三方 deps 不在 base image → odoo 起不來 / 模組裝不起來

local 模式 base image 是 vanilla `odoo:18.0`，**不含專有 addon**：

```
sh_subscription / sh_subscription_wallet / payment_ecpay_ecpg / ecpay_invoice_tw
```

這些是 `woow_paas_platform` 的 `depends`。要把 `PAAS_CI_ADDONS_PATH` 指向 `paas-odoo-ci/addons` 的 checkout（本機在 `/Users/eugene/Documents/woow/woow-paas-repo/paas-odoo-ci/addons`），讓 sandbox 把它 mount 進 `/mnt/extra-addons-ext`。沒設時 `/mnt/extra-addons-ext` 不存在 → odoo crash、模組裝不起來 → 回到成因 1 的 0 tests。

### 3. bare `--test-tags <module>` 不命中 → 0 命中

Odoo `--test-tags` 的語意：bare token（`woow_paas_platform`）被當成「**tag 名**」。專案沒有叫這名字的 tag → 0 命中。要過濾「某模組的全部 test」必須用 **前綴斜線** 的 module filter：

```
--test-tags /woow_paas_platform
```

`/<module>` 才是「跑這個 module 的所有 test」；bare `<module>` 是「跑掛了這個 tag 名的 test」（不存在 → 0）。

## How to avoid

跑完 sandbox 測試，**不要只看綠 banner**，逐項確認：

1. **看 log 的 `N tests` 計數，不是 banner**。log 出現 `... of 0 tests` = 沒跑到，不管 banner 什麼顏色都不算過。要看到非 0（如 `of 1142 tests`）才算真的有跑。
2. **首裝用 `-i`**（`-i woow_paas_platform`），或先 `kubectl exec` 查模組 `state=installed` 再用 `-u`。對 uninstalled 模組 `-u` 是 no-op。
3. **設 `PAAS_CI_ADDONS_PATH`** 指向 `paas-odoo-ci/addons` checkout，否則第三方 deps 缺、odoo 起不來、模組裝不起來。
4. **用 `--test-tags /<module>`**（前綴斜線），不要 bare `<module>`，否則 0 命中。

### `--without-demo=all` 的偽 regression（判斷 regression 的注意事項）

`--without-demo=all` 會讓約 **64 個 billing / subscription 測試 fail**，典型訊息：

- 找不到 sales journal
- `sh_journal_id` NOT NULL 違反
- `Subscription journal not configured`

這是 **環境問題（缺 demo data），非程式回歸**。判斷某次紅是不是真 regression 時，看**失敗 test 檔的領域是否與本次改動檔交集** —— 全部落在 billing/subscription 而你沒動那塊，就是環境問題不是回歸。

### woow-test.sh / CLAUDE.md 修正（另以小修分支處理）

本 pitfall 對應兩處要修，已標記另開小修分支處理：

- `.claude/skills/k8s-dev-sandbox/scripts/woow-test.sh`：`--test-tags "$MODULE"` → `--test-tags "/$MODULE"`，並在結果判斷補「看 log `N tests` 計數，不是只看 exit code」的提醒（防 0 tests false green）。注意這只修「tag 語意」與「假綠提醒」兩半，**修不了主因**（uninstalled 模組下 `-u` 仍 no-op，tag 改對也是 0 tests）—— 真正擋住 false green 的是先確認 `state=installed`（首裝用 `-i`，見上方 §How to avoid item 2）。
- `CLAUDE.md`「Run tests for this module」區塊：`--test-tags woow_paas_platform` → `--test-tags /woow_paas_platform`。

## Related

- 同 family（驗綠範圍 / 證據不對 → 漏抓）：
  - [[tag-scoped-test-misses-full-u-failures]] — tag-scoped 驗綠漏抓 full-`-u` 失敗（本篇是「0 tests 也報綠」的更前一步：根本沒裝起來 / tag 不命中）
  - `ci-workflow-container-missing-node.md` — CI 「pytest 從沒跑到」的 false green（本篇是 sandbox 版的「test 從沒跑到」）
  - `verification-before-completion`（superpowers skill）— 看實際 output（`N tests`）才算數，不是 banner 的綠
- 同 cycle 的 worktree 開發環境陷阱：`decouple-project-frontend-worktree-dev-pitfalls.md`
- 上游語意：Odoo `--test-tags` module filter 需 `/<module>` 前綴；`-i`（install）vs `-u`（update 已安裝）對 uninstalled 模組行為不同（`-u` no-op）。
