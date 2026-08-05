# project 559 helm values field acl landing

> 自 Claude memory 遷移（2026-08-05）。原 slug `project_559_helm_values_field_acl_landing`。盤點依據見 [memory-inventory-2026-08-05](../memory-inventory-2026-08-05.md)。
>
> 原 description："#559/PR#569：controller 層遮罩不涵蓋 ORM 端點（層級錯位）；覆核九項全過但因 Gitea 寫入全掛而無法落地到 PR"

2026-08-04。**#535 的延伸**：#535 把服務詳情 API 的憑證遮罩改成 spec-driven（controller 層），
但 `cloud_service.helm_values` 是裸欄位、無 `groups=` ⇒ 租戶用 `call_kw` 送 `read(['helm_values'])`
**繞過整個 controller 遮罩層**拿到明文。**鎖了前門，側門還開著。**

## 🔑 形狀：不是「漏了一條路徑」，是**層級錯位**

防護做在 **controller（應用層）**，資產的授權邊界在 **ORM（資料層）**。
⇒ **只要防護做得比資產的最內層邊界更外面，就必然存在一條繞過它的路**——不論那層寫得多完整。
第二條路徑**根本不在你修改的那個檔案裡**，所以逐檔審查看不到。

這是 repo 既有守則「授權/配額檢查必須落在最內層」的**第四個實例**（前三：#152 UI 擋 model 沒擋、
#289/#282 controller 擋 model 沒擋、#383 app 層擋 admission 沒擋），**而且發生在守則已寫下之後**。
⇒ 守則存在≠會被套用。新增遮罩時要主動問：**這個資產除了我修的這條路，還有沒有別的入口？**

同源：leader1 的 `create()` 閘門（`cloud_service.py:2741-2744` 自述「防意外、不是安全邊界」）
可被一次 `write({'subscription_id': False})` 繞過——閘在應用層、繞道走 ORM，同一形狀。

## 修法要點（PR#569，`4e76e1d0`，manifest `.109`）

- 欄位加 `groups='base.group_system'`（與同 class 其他五個載密欄位一致）
- 合法路徑集中成 model 層兩個具名入口 `_helm_values_dict()` / `_apply_helm_values()`，
  **controller 新增 0 個 `.sudo(`**
- 🔴 **`_create_service` 刻意不 sudo 整個 create**——那會連 ir.rule 的 create 檢查一起繞掉，屬 #519 範圍
- 🔴 **遮罩鏈必須保留**：`paas.py:3810` 仍 `_sanitize_helm_values(service._helm_values_dict(), ...)`。
  提權讀出的是明文，此處若解壞＝**用 #559 換掉 #535**。這是 rebase 覆核的第一檢查點。

**repo-lint 的 `controller-sudo` gate 不會亮，而那是正確的**：該 gate 的 invariant（`repo-lint.py:203`
逐字）是「controller 的 `sudo()` 會**繞過 ir.rule**」＝**記錄級**；本案是已授權單一 record 上的
**欄位級**提權。⇒ **gate 不亮 ≠ 沒有提權發生**，要在 PR 寫明提權位置，否則下一個人會誤讀。

## 🔴 第二個發現比原 issue 大：測試對「租戶身分」系統性零覆蓋

只加 `groups=` 後，服務詳情與 reconfigure 全部 `AccessError`（`groups=` 連平台自己的 ORM 屬性存取
也一併擋，而那幾個 controller 方法都以**租戶身分**執行）——**而全套測試零紅**。
成因：既有測試幾乎都以 root 呼叫 controller helper。⇒ 補 `TestTenantFacingPathsStillWork`
（每條顯式 `with_user`）是在補系統性盲區，不只是本單迴歸。

## flaky 測試（會害到所有人，修法一行、不在任何人本單）

`TestProvisionUniqueIndex.test_partial_unique_index_present`（`test_ha_paas_provision.py:20-29`）：
`indexname LIKE` 萬用字元命中 **2 筆**（partial unique index ＋ Odoo 對 `index=True` 自動建的普通索引），
**無 `ORDER BY` 卻斷言 `rows[0][0]`** ⇒ 先拿到哪筆由實體列序決定。
⇒ **會隨機讓任何以較舊 DB 當 baseline 的 PR 變紅，症狀看起來像「索引不見了」**，易誤診成 schema 問題。
⚠ 歸因時我猜的「partial unique index 沒建起來」**與事實不符**——三個 DB 都有那兩個索引。
**乾淨 DB 過了 ⇒ (b) DB 狀態」這個判準只對了一半**，要再往下一層才看到真因。

## 🔴 環境：Gitea 寫入全掛時，錯誤訊息是空的

2026-08-04 覆核完成當下實測：`GET` 200 正常，**所有 `POST`/`PATCH` 一律 500**
（改標題、留言、換 curl／python、換另一個 issue 皆同）⇒ 全面寫入故障。
**500 的 body 是 `{"message":"","url":".../swagger"}`，`tea` 則回 `Error: could not edit pull request: `
——兩者都把 5xx 吞成空字串。**

⇒ **看到空的錯誤訊息不要當成「沒事」或「間歇」，那正是 5xx 被吞掉的樣子。**
（實作者稍早送留言時遇過一次空 `Error:`、重試成功——那是這次故障的前兆，被當成間歇而略過。）

⇒ 連帶失效：**「PR 預設帶【勿 merge】、通過才移除」這個機制，在「移除的動作本身做不到」時
會把標示卡在錯誤狀態上**——看起來像覆核未完成，實際是 API 壞了。此時要用**不經該系統的載體**
（maestri 訊息／memory）承載狀態，並明說「以此為準」。

相關：[[feedback_absence_conclusion_scope_not_universe]]、[[feedback_negative_verification_needs_positive_control]]、
[[reference_gitea_approval_gate_5xx_orphan_issue]]、[[reference_tea_cli_write_gotchas]]、
[[feedback_preserve_findings_before_dismiss]]
