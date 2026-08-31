---
title: 帳務 incident 人工查證與關單 runbook
category: guides
status: active
created: 2026-08-30
updated: 2026-08-31
related:
  - docs/decisions/864-wallet-charged-not-credited.md
---

# 帳務 incident 人工查證與關單 runbook

## 適用範圍

本 runbook 只處理 Odoo 後台 **Woow PaaS → Billing Incidents** 中 `state` 不是
`resolved` 的記錄。操作角色是 Odoo role `account.group_account_manager`，絕非任何
Workspace role。以下三句是 comment #11112 的 durable security contract，保留 English
逐字詞組供 regression 鎖定：

- Odoo role `account.group_account_manager` (not a Workspace role)
- all incidents across all companies
- all current managers in that Odoo group
- no company M2O/rule; base.group_system behavior is not narrowed

`woow.billing.incident` 是刻意設計的 **global finance ledger**，不是 company-owned
業務表：它沒有 company M2O／company record rule，也不另行縮減既有
`base.group_system` 系統行為。這句不構成額外授權：`base.group_system` alone does not grant
incident read/write/menu access；只有**目前仍在** `account.group_account_manager` 的帳號可
存取，即使是 system-only 帳號也不例外。每一位該 Odoo group 的現任 manager 都可跨全部
company 讀取全部 incident，也可依 model allowlist 保存兩個人工查證欄位；這是 Eugene 裁決
的安全契約，不是待補的隔離缺口。`orphan`／`needs_human` 的專屬 review activity 同樣派給
該 Odoo group 內**全體現任** managers，不依使用者目前 company 篩選。初次 schedule 派
全體；後續 incident schedule／duplicate `_log()` 在同一 advisory lock 內收斂 roster。另有
只針對該 manager group `users` 變更的 `res.groups` hook，在同一 transaction 收斂**所有
unresolved incidents**：撤掉離組者的 dedicated activities、移除 duplicate，再替新進
manager 補到每人恰一筆 active dedicated activity；其他 group 的 users 變更不觸發。角色
撤銷後，即使 stale activity 仍 assigned、incident message 的 author/create_uid 仍是本人，
`mail.activity`／`mail.message` 的 direct-ID read/search/action gate 也要求當下 group membership。
本階段不靠 cron 才達成 role revocation。

### Fresh evidence（2026-08-31；僅佐證現況）

- prod/stg each currently has 1 `res.company`
- internal account managers: prod=1/stg=3

以上是 current-state evidence only；policy does not assume single-company。即使未來任一環境
增加 company，本節的跨全部 company 讀取與全 group 派送契約仍不變，絕不能把上述數字
誤讀成加入 company M2O／rule 的前提。

畫面上的警告是本流程的不變式：

> topup 記錄可能已被回捲、在 Odoo 裡查不到是預期現象，不是關單理由

## 唯一合法動作

1. 以 incident 的 `external_ref`（payment incident 是原始 ECPay MerchantTradeNo）到
   **ECPay 後台查證**。
2. 回到 incident form，在「人工查證」選擇且只能選擇一個結果：
   - **Found in ECPay**（`ecpay_found`）
   - **Not found in ECPay**（`ecpay_not_found`）
3. 在 `resolution_note` 寫下查證依據與結論。不得只寫「Odoo 查不到 topup」。
4. 按 **「人工已處理」**。

按鈕只會保存 `manual_check_result`、`resolution_note`、`resolved_at`、
`resolved_by_ref` 並把 incident 關成 `resolved`；它**不會修改 wallet、不會補點、也不會退款**。
關單前 account manager 只能修改前兩個人工欄位；每次 generic `write()` 都會先對完整
recordset 做 `FOR UPDATE` 並重讀 state。`sudo()` 不構成 lifecycle 權限；generic ORM
`create()`（含 `sudo().create()`）也不能鑄造 ledger row，production 建立只走 `_log()` 的
raw atomic upsert。測試 fixture 與第一次關單各自只由模組內、JSON-RPC 無法表達物件身分的
private capability 開門；後者只能執行一次精確的 `unresolved → resolved` transition。
一旦 `state = resolved`，整筆 first resolution audit 完全 immutable，連這兩個人工欄位也
不得再改；DB 同時要求 resolved row 的 `resolved_at` 非空。incident ledger 也一律不可
`unlink`（一般 `sudo` 與 RPC 偽造 context 皆不例外；只相容 Odoo 真正 module uninstall
流程）。

ECPay **查得到或查不到都只代表「查證結果已記錄，這張 incident 已關單」**。兩種結果都
不得宣稱按鈕已補償款項，也不得把「關單」寫成「款項已補回」。

## 禁止動作

- ⛔ 不得直接修改 `loyalty_card.points`。
- ⛔ 不得用 `refund_wallet_points` 代替補點或 incident 關單。
- ⛔ 不得自行建立 `adjustment` ledger、手改 wallet transaction，或以其他臨時方法送點。
- ⛔ 不得因 Odoo 查不到 topup 記錄就略過 ECPay 查證。
- ⛔ 不得把 `ResultData`、卡號或其他卡片密文貼進 `detail`／`resolution_note`。

`_log(snapshot)` 的 field allowlist 只是一道 **shape boundary**，不是完整 PCI
boundary。共用 sensitive validator 會套到 `_log` 的所有文字 ingress、manager
`resolution_note` RPC write、人工關單按鈕與 `_resolve(resolution_note)`：明確的
`ResultData`／`Card4No`／`Card6No`／`CardNo`／`CVV`／`CVC`／`CID`／`CSC`／
`security code`／`SAD`（含 `sensitive authentication data`）／`PIN`／`PIN block`／
`track1`／`track2`／`magnetic stripe`／`PAN` marker，以及中文「安全碼／卡片安全碼／
信用卡安全碼／PIN碼／PIN區塊／磁條／磁條資料／軌道資料／授權碼」與 Luhn-valid 13–19 位
PAN-like 數字都會被拒絕。所有 caller-controlled incident-owned free text（`detail`、
`resolution_note`、activity feedback）先在 raw、HTML entity-decoded 與 visible plaintext
三層掃描；只要出現 HTML tag／comment／quoted attribute／entity markup 就 fail-closed，
因本產品只接受 plain-text 理由。通過 markup gate 後，validator 才做 HTML entity decode、
`html2plaintext`、Unicode NFKC，並以 Unicode letter/digit 作唯一邊界：**所有**非
letter/digit 字元一律轉成同一 separator，再跑 marker／PAN Luhn。因此卡號被 Mc（如
U+0903）、So／emoji、Sk、Cf、Mn、Me、點號、全形字元、U+FE0F variation selector、
U+200B zero-width character 或 combining mark 拆開都不會繞過；合法中文查證理由只要不含
上述 marker 仍可通過。只有專用
`external_ref`／`trade_no` 欄位可豁免「純數字碰巧通過 Luhn」；兩欄仍拒絕上述 marker，
並另有長度、安全字元與 CRLF/control-character shape gate。`external_ref` 只接受 1–20
字元 raw provider MTN，
或 `<namespace>:<safe-reference>`；`trade_no` 只接受 provider-safe 字元。合法 20 碼
numeric MTN 仍可原樣顯示在 activity／後台，但輸入先經 shape 驗證，HTML 輸出再 escape；
任何 error/collision log 只記 incident id、field 與 category，不回印 raw reference，也不留
可枚舉的 fingerprint。自由文字中的 `MTN:`／`TradeNo:` prefix **不是豁免**。caller 仍有責任只交付
必要、去敏的 `detail` 安全摘要；不得把「通過 validator」解讀為 payload 已符合 PCI。

上述限制避免人工動作與後續自動化重複給點；任何需要改變 wallet 餘額的產品流程不屬於本
runbook，也不是「人工已處理」按鈕的功能。

## 稽核欄位

關單後確認以下欄位均存在：

- `state = resolved`
- `manual_check_result` 是 `ecpay_found` 或 `ecpay_not_found`
- `resolution_note` 非空
- `resolved_at` 非空
- `resolved_by_ref` 是執行者的純值 user id

系統只會移除這一筆 incident、且 activity type 精確為
`woow_paas_platform.mail_activity_type_billing_incident_review` 的 activities；同一筆
incident 的 generic Todo／其他 activity type，以及其他 incident 的待辦都會保留。
專屬 type 已限制 `res_model = woow.billing.incident`，是 schedule、dedupe、cleanup 的唯一
provenance；summary 不是識別依據。billing incident model 或此 dedicated type 的
`mail.activity.create()` 只能由模組內 scheduler 的 object capability 執行；manager、`sudo` 與
RPC truthy context 都不能自行鑄造。既有 targeted activity 的 generic `write()` 也全拒（包含
`active`、note／summary、`res_id`、type、assignee），操作人應回 incident form 查證與關單。
dedupe 只看 active 的專屬 activity：inactive／standard `action_done()`／`unlink()` 若 incident
仍 unresolved，會在同一 transaction、同一 advisory lock 機制下立即收斂成**每位 manager
恰一筆** active alert；不必等下一次 duplicate observation，而且完成 activity 本身**不會**把
incident 關成 resolved。terminal flow 進 scheduler object capability 前會用 allowlist 重建
乾淨 context，完全丟棄 caller `default_*`；scheduler create 同時明寫 target、type、assignee、
content、deadline、`active=True`、`attachment_ids=[(5, 0, 0)]` 與其他 security-sensitive 欄，
因此 RPC 的 `default_active=False`／`default_attachment_ids` 無法污染重建活動。activity 的
summary／note 會依每位 manager 的 `lang` 分別翻譯，
`kind` 顯示翻譯後 selection label，不顯示 raw key。

ledger 的 source/business 欄位仍完全 relation-free。為使用 Odoo 標準
`mail.activity.create()`／notification bus／`action_done()`／`unlink()`，model 繼承
`mail.thread`；其 follower/message relational metadata 只指向已由第一個獨立交易 commit
完成的 incident。這與禁止 outer transaction source row 形成 dangling FK 的約束正交：
前者是 Odoo mail infrastructure metadata，後者才是禁止加入 ledger 的 business/source
Many2one／Many2many。

`mail.thread` 帶來的 incident-owned chatter ingress 已刻意縮到唯一 core
`mail.message_activity_done` source：一般 account manager 的自由 `message_post()` 全拒；任何
user feedback/body 都先走 plain-text markup＋PCI validator，`attachment_ids`／`attachments`
只要非空也直接拒絕，因本階段不掃描檔案內容。驗證完成後才把 JSON-RPC 無法偽造的
module-private object capability 交給 `message_post_with_source()` 呼叫 `super`，所以 Odoo core
產生的受控 activity template HTML 可以通過，但 caller markup 不會。

related-model 也在同一 model 檔 targeted 守線：`mail.message` 只有上述 capability 能建立
`model=woow.billing.incident` 的完成訊息，generic create／write／unlink（含任何 body、PAN 或
`ResultData`）全拒；其 read/search 也要求 current account-manager，不接受 author/create_uid
捷徑。`ir.attachment` create／write 在 payload 落 DB 前拒絕直接綁 incident、dedicated
billing activity 或 incident-owned message，既有 unrelated attachment 搬往這些 old→new
與 ORM final target 也原子拒絕。protected carrier 的 `res_id` 先 canonicalize：canonical
positive numeric string 轉 int 後再查 target；float、bool、zero、leading-zero／帶符號／空白等
noncanonical 值一律 fail-closed；合法其他 model control 不受影響。其他 model 的
message／attachment 行為不受影響。這些 private context keys 全部登錄在 repo
`INTERNAL_CTX_KEY_REGISTRY`，值只接受 object identity。
