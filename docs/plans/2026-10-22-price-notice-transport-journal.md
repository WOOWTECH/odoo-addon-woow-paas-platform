# Dormant SMTP acceptance journal implementation plan

**Goal:** 可審查、未接線的單地址 SMTP DATA acceptance journal；不授予公告、寄信或切價權限。

**Architecture:** 獨立純 Python service 提供明確呼叫才使用的 PostgreSQL DB-API journal 與 SMTP subclass。先獨立 commit attempt，再於 SMTP 前獨立 claim 為 unknown；只在單一實際 envelope RCPT 接受且 DATA 250 後獨立 commit acceptance。模組不加入 Odoo imports、model、cron、RPC、queue 或七天 gate。

**Tech Stack:** Python standard library smtplib/dataclasses/hashlib；PostgreSQL DB-API connection factory（由未來受信 adapter 注入）。離線 unittest 使用真 SQLite 交易加 placeholder adapter，僅驗證 portable SQL 與交易邊界，不宣稱 PostgreSQL/Odoo 整合已驗證。

---

1. 新增 `src/services/llm_price_notice_transport.py`：不可變 attempt validation、獨立交易、唯一鍵與冪等 acceptance；schema 只能由未來核可 migration 安裝，不於 import/constructor 建表。
2. 同檔新增 dormant SMTP/SMTP_SSL subclass：精確 bytes SHA256、單一 envelope、RCPT 與 DATA 狀態；沒有 observed DATA 就沒有 receipt。claim 後任何錯誤維持 unknown，不自動重送。
3. 新增 `tests/test_llm_price_notice_transport.py`：fake SMTP protocol，不開 socket；成功、RCPT/DATA/連線失敗、test bypass、重複、外層 rollback、journal failure 與額外收件者。
4. 執行 `python -B tests/test_llm_price_notice_transport.py -v`、既有 dormant notice tests、ruff F 與 `git diff --check`。記錄結果與 PostgreSQL/Odoo/dispatch 權限等缺口。本任務不 commit/push、不改 Gitea/叢集。

## 交付邊界

讀過外層 DECISIONS.md、#1936 既有 model/tests 與 `../price-notice-smtp-hook-readonly-20261022.md`。實作不改 #1936 immutable draft、不匯入 dormant checker；沒有 publication/approval bridge。SMTP 已接受到 journal commit 的 crash gap 無法消除；unknown 要人工 relay 對帳。SQLite 測試不能替代實際 PG 並發/DDL/Odoo cursor 測試，正式接線前必須補上。


## 實作與驗證（2026-10-22）

- 新增未註冊的 `src/services/llm_price_notice_transport.py`；既有 ledger/model、services imports、mail queue、checker、七天時間及產品 UI 完全未接線。SCHEMA 是未執行的 PostgreSQL DDL 草稿，不會自動建表。
- `Journal.prepare` 先用專用連線獨立提交 snapshot；相同 UUID 不同內容拒絕，重複相同內容不改原狀態。`_claim` 以 conditional UPDATE 獨立 commit 為 unknown 才准 SMTP；第二個 session/重試不能再寄。只在 RCPT 250/251、同一精確 bytes SHA256、DATA 250 時呼叫 `_accept` 獨立 commit，重複事件保留第一個觀測時間。Message-ID/mail state 不是輸入。
- 提供 SMTP 與 implicit TLS subclass，不於 constructor 連線（也避免 DNS lookup），不呼叫 connect/login/send。未來 Odoo adapter 必須在 `ir.mail_server.connect` 建立這類 session 並綁事先提交 attempt；不能拿一般 session 的 send_email return 來補 receipt。`_postprocess_sent_message` 最多讀已提交 journal，不能憑自己的 hook invocation 創造 acceptance。
- payload digest 必須由最後序列化的 MIME bytes（CRLF、headers、attachments 都固定）產生，而非只 hash 公告正文。單一最終 SMTP envelope 必須完全等於 attempt.destination；顯示名、多地址、額外 CC/BCC 均不能繞過。receipt 依 UUID 綁不可變完整 attempt，沒有引用 mail.mail FK，auto_delete 不影響它。
- 執行 `PYTHONDONTWRITEBYTECODE=1 python3 -B tests/test_llm_price_notice_transport.py -v`：21/21；既有 `tests/test_llm_price_notice.py -v`：17/17。`ruff check --select F --no-cache` 與新檔 `git diff --no-index --check /dev/null <file>` 通過。測試沒有 SMTP socket、叢集操作或真實信箱。

### 明確未完成的接線與保證

本切片沒有安裝 PostgreSQL schema／角色或 migration、沒有產品可呼叫的 connector/dispatch、沒有 Odoo transaction cursor 接線、沒有 owner/legal/publication authority；不構成 GA readiness。未來 connection factory 必須每次回傳專用的新 PG connection，不能重用外層 Odoo connection。DB role 須限制 snapshot/receipt 變更；目前只有 service API 的不可變／冪等約束，不聲稱可抵禦 DB owner 或任意 Python 呼叫者偽造內部方法。

本機無 PostgreSQL 工具；SQLite placeholder adapter 用真獨立交易驗證 rollback/commit 與唯一鍵，但不能驗證 PG concurrent row locks、隔離層級、commit response lost 或 Odoo registry/auto-delete 行為。outer rollback 測試因 SQLite database-wide write lock，外層先 BEGIN、journal commit 後才寫 unrelated row 再 rollback；**未驗證外層已持鎖的 PG 情境**。兩個 session 的 claim 測試是交錯執行，非 PG 多程序並行測試。正式接線前必須補 PG/Odoo integration suite。

SMTP DATA 250 已觀測到 journal commit 之前仍有 crash gap；commit 失敗或 response lost 不自動重試，不捏造 acceptance。DATA timeout/disconnect 保持 unknown；須用可信 relay 證據人工對帳。SMTP acceptance 不代表 inbox delivery，完全不啟動七天倒數。新建另一 UUID 是否准寄須由未來受信 dispatcher/owner 決定，本模組不提供自動重送功能。
