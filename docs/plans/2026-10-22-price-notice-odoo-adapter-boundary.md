# Dormant Odoo 18 / PG adapter boundary

此 bounded slice 不註冊 hook、model、migration、cron 或 RPC，不連 SMTP、不變更七天 gate。工作分支為 `feat/llm-price-notice-transport-journal-dormant-1022`，已重定基到 #1936 新 head `ac5293fde3559e1380b39f6c9f6bc70dba805cbc`；本切片不修改該 PR 或 Gitea。

## 目標與實作

1. 新增未匯入的 `src/services/llm_price_notice_odoo_adapter.py`，提供 Odoo registry cursor → journal DB-API connection adapter、不可變 mail binding、committed ORM preflight。
2. 新增 `tests/test_llm_price_notice_odoo_adapter.py`，以合成 ORM/PG cursor 與真 SQLite 交易測試 fail-closed contract。既有 transport 與 dormant checker 保持未接線。
3. 驗證聚焦測試、既有 transport/notice 回歸、ruff F、新檔 whitespace、分支與 diff 範圍。

依據 `../price-notice-smtp-hook-readonly-20261022.md`：部署核心 mail_mail.py:270 的後處理在成功/失敗都呼叫、:305 可能刪 mail；:754 呼叫 send_email，ir_mail_server.py:739 test bypass 不發 SMTP 卻回 Message-ID，:747 的 smtplib 呼叫丟棄 refusal map。因此本 adapter **沒有 acceptance writer**。後續 hook 僅能讀 transport 已提交的 journal，不能由 hook、mail.state、Message-ID 或 mail 消失反推接受。

repo 參照：`src/models/llm_event.py` `_llm_alert_queue_email` 使用多地址並排 cron；其 `_llm_alert_confirm_emails` 的既有狀態推論不適用本調價回執。`src/models/workspace_invitation.py` 為 template/queue 路徑；`src/models/llm_backoffice.py` 展示獨立 registry.cursor 慣例。本切片不修改這些行為。

## Contract 與未執行 schema

- `MailBinding` 綁 immutable `Attempt` 全欄位、database_name、mail.mail ID、recipient obligation ID。attempt 已涵蓋 campaign fingerprint、environment、recipient key、destination/revision、最終 MIME bytes SHA256。
- `OdooJournal.bind` 要求同一 snapshot 的 attempt 已在另一已提交交易可見且仍 prepared。`BINDING_SCHEMA` 僅供 migration 審查，不會執行：attempt_uuid PK/FK、同一 `(database_name, mail_id)` 只能綁一 attempt。沒有 mail.mail FK，避免 auto_delete 級聯刪回執或持有外層 mail 鎖。相同 binding 冪等；變更 mail ID、obligation、database 或 snapshot 拒絕。
- `RegistryConnectionFactory` 每次明確呼叫才開新 registry.cursor，比對 `current_database()`/`pg_backend_pid()`，拒絕外層 cursor、共享 backend 或跨 DB。DB-API cursor view 的 close 不會提前關閉 Odoo cursor；由 connection adapter 在 commit/rollback 後 close。只查詢外層 backend 身分，不 commit/rollback/close 外層。若發現共享 backend 的測試 wrapper，不自行 close（避免回滾 caller）；wrapper 清理由其原 owner 負責。
- `odoo_boundary(outer_env, service_uid=..., runtime_environment=...)` 是未註冊的明確 server entrypoint；constructor 不開 cursor，不寄信。只有呼叫此函式才 import Odoo。未來由受信 server 設定指定 service UID 與環境，用 `api.Environment(new_cr, service_uid, {})` 讀 committed rows，沒有 sudo 或 request context。actual `modules.module.current_test` 在每次操作即時轉 bool 檢查，test mode 在 DB 查詢之前拒絕。
- `bind` 與 `preflight` 都重新讀獨立 cursor 的 mail/obligation/campaign，拒絕不存在（含未提交）記錄；要求直接 canonical 單一 email_to、空 email_cc/recipient_ids；比對 campaign fingerprint/environment、recipient key、地址快照與 revision。最終 envelope 必須是只含 snapshot 地址的 tuple，最終 MIME bytes 必須 hash 相符。不得從 mail headers 猜 envelope；未來必須在 Odoo 最終 envelope/MIME 序列化位置提供實際值。
- `preflight` 也從獨立 journal 重新讀 persisted binding，要求 prepared；unknown/accepted 都拒絕再 dispatch。成功只回 immutable binding，不建立 SMTP session、不 claim、不記 acceptance。即使 Python 偽造相同 dataclass，也不是 owner/legal/dispatch authority。

## 驗證

最初的離線測試沒有 Odoo module 或本機 psql/postgres/initdb；測試以 socket guard 禁止 SMTP connection，fake ORM 的 mail.state/message_id getter 會直接拋錯，正常 matching case 仍通過。其後**在獨立、精確標記的 llmdev scratch DB** 補了真 PostgreSQL DDL／兩個 session 並發 claim／唯一約束，以及真 Odoo SQL cursor 的 `OdooJournal` prepare/bind/claim/accept 合成 API 與外層 rollback 對照；測試 DB 已刪除、獨立查核不存在。這不是註冊 addon 或真 ORM mail／義務測試，也沒有 SMTP、Secret 或 Gitea 寫入。

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -B tests/test_llm_price_notice_odoo_adapter.py -v
PYTHONDONTWRITEBYTECODE=1 python3 -B tests/test_llm_price_notice_transport.py -v
PYTHONDONTWRITEBYTECODE=1 python3 -B tests/test_llm_price_notice.py -v
/data/pi-agent/venv/bin/ruff check --select F --no-cache src/services/llm_price_notice_odoo_adapter.py tests/test_llm_price_notice_odoo_adapter.py
git diff --check
```

結果：adapter 21、transport 21、notice 17，共 59 個離線測試通過；ruff F、tracked diff/new-file whitespace 通過。真 PG／Odoo SQL cursor 的有限路徑見工作區 `impl/price-notice-pg-contract-run-20261022.md`；它沒有驗證 Odoo ORM `OdooMailBoundary.bind/preflight`。新 adapter 的 dormant import 邊界包含在離線測試中。

## 接線前仍缺

- 核可的 PG migration/schema 版本、snapshot/receipt 防修改角色與 DB 權限、service UID 的 mail/obligation read ACL/record rules；目前不防 DB owner 或任意受信 Python 呼叫內部 API。既有 #1936 state 仍 draft-only，沒有批准/dispatch 權限。
- 剩餘真 PG 驗證：真正 Odoo registry pool（不是合成 registry wrapper）的權限、Repeatable Read 與 serialization failure、並行 bind、外層已持 Odoo mail 鎖時 journal commit/rollback、commit response lost。兩個真 session 的 claim 與 schema 約束已測，但只覆蓋合成資料。
- 真 Odoo 驗證：fresh Environment/cache/ACL、未提交 mail/obligation 不可見、實際 MIME/附件/CC/BCC/partner 展開及 payload hash 穩定性、auto_delete 後 binding/receipt 保存。fake ORM 缺列測試只驗證 contract。
- 本 slice 尚無「preflight 到 SMTP claim」的 writer interlock：mail、obligation、MIME 與 binding 檢查後可能併發改變。未來 dispatcher 必須建立 immutable outbox/最終 bytes 與身份綁定、在 transport claim 做相同快照校驗並互鎖所有 writer；不能把這次 preflight 當可重放的寄送授權。
- 未接 `ir.mail_server.connect/send_email` 或 `_postprocess_sent_message`；不得將一般 SMTP session 的回傳值補寫成 acceptance。llmdev 現役 Odoo 18 的 `connect` 回傳 `SMTPConnection` wrapper、內部才是 `smtplib.SMTP`；`mail.mail.send` 為批次共用 session，`_prepare_email_message` 會重算 envelope／刪 Bcc／可能改 From。因此本切片 `AcceptanceSMTP` **不會被 Odoo 自動使用**，且 payload digest 必須綁在最後準備完成的 MIME bytes。下一切片需專用單封 dispatch/session，不得 monkeypatch 全域或混入一般 mail batch；見工作區 `impl/price-notice-smtp-connection-shape-20261022.md`。只有既有 observer 的 RCPT + DATA 250 路徑能提供觀測。DATA250→journal commit crash window 仍存在，unknown 要以可信 relay 記錄人工對帳，不自動重送。
- 未提供 publication/owner/legal 及 new-api pricing writer gate，也不啟動七天時計、不解除 GA blocker。

## 單封 Odoo SMTPConnection 包裝器草稿（2026-10-22）

在相同未註冊分支新增 `src/services/llm_price_notice_odoo_smtp.py`。llmdev 現役 Odoo 核心已唯讀確認：`SMTPConnection` 把 `sendmail`／`rcpt`／`data` setter 轉送至內部 `smtplib` 物件；`ir.mail_server.send_email` 在最終 `_prepare_email_message` 後呼叫 `smtp_session.send_message`；`mail.mail.send` 會在批次共享 session。故草稿 `BoundNoticeSMTP` 僅接受由未來受信任派送器持有的**專用新 session**，一次性替換該實例的三個傳輸方法，於 `send_message` 真正序列化後的 bytes／唯一 envelope 上重讀 committed source/binding，再獨立 claim。只有相同單一 RCPT 250/251 及 DATA 250 後同步提交 journal acceptance，`finally` 恢復方法。未觀測 DATA 的假成功會拒絕；外層 Odoo 的 Message-ID／mail state 仍不是證據。

離線 `tests/test_llm_price_notice_odoo_smtp.py` 用真 `smtplib.send_message/sendmail` 與仿 Odoo property-delegating wrapper、無 socket 的合成協定覆蓋成功、RCPT/DATA 拒絕、最終 MIME 或收件人漂移、committed preflight 拒絕、假成功與 journal 提交失敗。這不是 Odoo registry、真 PostgreSQL、真 SMTP 或 relay 回執測試；沒有呼叫 `ir.mail_server.connect`、`mail.mail.send` 或任何現役寄信。測試只確認這個有界 transport observer 的形狀。

**接線前硬前置：** 專用 session 必須由受信 server 以已批准 mail server 設定建立與關閉，不能傳入 `mail.mail.send` 的共享批次 session；mailer 要在源資料 writer lock 與已批准公告／受眾下取得最終 Odoo MIME；即使 preflight 通過，現有程式也沒有跨 writer 互鎖，mail/義務仍可能在 claim 前後改變。需於真 Odoo／PG 隔離資料庫驗證 `_prepare_email_message`、單封 `mail.mail` 展開、MRO 覆寫鏈、權限、鎖次序、auto_delete、測試模式及崩潰窗口；再接受控 SMTP sink，最後由負責人／法務核可正式發佈。此草稿仍不能寄送真通知或啟動七天。
