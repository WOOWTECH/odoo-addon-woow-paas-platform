# LLM-12b I2 跨版本安裝與升級驗證 — 2026-10-21

此驗證只涵蓋未啟用的 I2 候選持久化與封存切片；不代表 I2/I3 或 LLM-12b 全票完成。
使用 `dev-sandbox-llmdev` 現有 Odoo 映像的獨立暫存資料庫，不更新正式站、aimodels、llmdev 既有資料庫或部署。

## 精確來源與流程

- 舊版來源 `cf3b6dd1fd7bb370f1f1ac4aacdbcb68b85a6681`；新版來源
  `489ee243ffca0b5111a260a823ba41bdfe699c5d`。驗證器以 Git archive
  固定兩版檔案雜湊，拒絕從工作樹讀取變動中的程式。
- 在驗證器建立並標記擁有權的 scratch DB 中，依序：舊版安裝、未設定狀態的
  18 個 LLM cron 測試、建立一列已提交的舊候選、升級至新版、資料與 FK 檢查、
  再次 cron 測試、15 個真 Odoo TransactionCase，最後刪除自己的 DB 與 overlay。
- `init()` 對舊候選回填 `account_ref`，不捏造舊 upstream ID 或未知的 L/U/known；
  兩個 account FK 均驗出 nullable 與 PostgreSQL `ON DELETE SET NULL`。
  舊樣本、精確整數與未設定狀態的 row IDs 在升級前後保留。

## 結果

- 固定來源的 15 個 Odoo 方法各執行一次，**15/15 通過、0 failure/error**。
  網路、HTTP、new-api、外部效果與其他資料庫存取計數均為 0。
- 舊版安裝、新版升級及兩次未設定 cron 探針均通過；兩次探針沒有新事件、
  incident、通知或信件，也沒有變動既有追蹤列。
- 驗證器回報 `migration_assertions_passed=true`、`db_removed=true`、
  `files_removed=true`。另以 `pg_database` 精確查詢確認該 scratch DB 計數為 0，
  以 pod 檔案測試確認該次 `/tmp` overlay 不存在。
- 20 項驗證器本機靜態／模擬檢查通過、SHA256 清單核對通過；
  `python3.13 scripts/audit/repo-lint.py --all` 與 `git diff --check` 通過。
- 工作區原始、僅含 allowlist 欄位的驗證器與結果位於
  `impl/llm12b-i2-scratch-validation-1021/run-live-migration-20261021-0424/`。

首次驗證在測試階段失敗，且已清理自己的 scratch 資源。原因是測試使用的
`MagicMock` 被 Odoo 誤判並快取為 ondelete hook，另有新帳戶 fixture 的狀態需要
先 flush；`489ee243` 只修正測試隔離與 fixture，產品邏輯未變。上述通過結果
來自修正後重新建立的另一個 scratch DB，不混用首次失敗的結果。

此切片尚無排程、AP 額度抽樣、I3 覆蓋、incident、跨工作者提交後重啟驗證或
staging 財務端對端驗收；須在後續切片完成後才能啟用或作為 GA 財務一致性證據。
