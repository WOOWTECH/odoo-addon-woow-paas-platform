# I2 閉戶候選證據保留（本機切片，未部署）

承接 `cf3b6dd1fd7bb370f1f1ac4aacdbcb68b85a6681`。目前執行中的 scratch runner
仍固定讀該 commit，不受本工作樹修改影響；本輪未編輯 runner、未執行任何叢集測試、
未 push／PR。I2／I3 仍未完成。

## 行為

- 新增唯讀、不可變的 `llm_reconcile_evidence`，只供 system read。
  沒有 cron、AP、HTTP、incident、alert 或 lifecycle write hook。
- `_observe` 碰到 closed／missing account，先把現存候選封存，再移除運作中候選，
  兩步位於同一 savepoint；任一步失敗會保留原候選。closed 不需要新的 balance sample，
  也不計算 ledger，不把 forfeited balance 判為 shortfall。重複處理不重複封存。
- 封存保留本地 account_ref、當時 upstream ID、首次 quota／used quota／request count／
  時間、最後接受樣本時間、direction、outcome、最後觀測使用的 L/U／signed known 差額。
  後三個 aggregate 以 decimal text 保留，允許超過 int64；樣本本身仍為 bigint。
  I3 coverage 明確為 unavailable，不是 zero 或已結案。
- 運作中候選綁定 upstream ID。遇到不同 ID 或 legacy 未綁定 ID，先封存舊候選，
  新樣本從新的五分鐘 baseline 開始；不複用舊時間／request count。
- 已觀察到 closure 後，同一 local/remote ID 再成為 active，也沒有可繼承的舊候選。
  正常 reopen 會建新 local/upstream account，即使尚未觀察舊帳戶 closure，也不繼承舊列。
- 不改 `llm_account.py`、controller、帳戶狀態機、帳戶欄位 immutable gate 或 unlink gate。
  原產品不允許修改已設定的 upstream ID；identity-change Odoo 測試用 owned fixture SQL
  模擬異常，沒有放寬產品守衛。

## 刪除／升級相容性

1. 候選 `account_id` 由 required＋ON DELETE CASCADE 改為 nullable＋SET NULL；
   封存證據同樣 SET NULL，並保留不可變的 `account_ref`／upstream identity。
   實體刪除不會透過這兩個 FK 抹掉觀測資料，也不額外阻止帳戶刪除。
   原 `UNIQUE(account_id)` 保留；NULL orphan 不會接到新帳戶。
2. `init()` 對 legacy 候選補 `account_ref=account_id`，不捏造歷史 upstream ID／L/U／known。
   未記錄的 ID 維持 0（unknown），未記錄的 text 保持空值（unknown，不是字串 '0'）。
   下次觀測會先以 identity_unbound 封存 legacy baseline，再重新建立。
3. **必須驗證真 cf3b6dd1 schema → 本切片 upgrade** 的 NOT NULL 移除、FK confdeltype='n'、
   account_ref backfill 及資料保留。單純「新 SHA install→同 SHA upgrade」不足以證明舊 FK
   的遷移；本輪未改 manifest／新增版本 migration，依 repo 規則由 registry schema update
   處理欄位變更，真升級若未更換既有 FK，需補顯式 migration，不能宣稱已驗。
4. 原帳戶產品 unlink 只允許模組卸載；本切片不改它。直接 SQL／其他 FK 可能仍有自己的
   restrict/cascade 規則，此切片不變更這些規則。Odoo 測試同時檢查 model metadata 與
   PostgreSQL 實際 candidate/evidence account FK 的 SET NULL，並不宣稱整體帳戶可刪。
5. 保留資料可孤立存在；未新增掃描／清理／人工結案工作流，不自動設保留期限。
   模組卸載仍可刪 model/table，**不是卸載後仍有財務證據的保證**；卸載前需按既有稽核
   流程匯出/保存。過去版本已經 cascade 刪除的資料無法由本切片復原。
6. 沒有帳戶 write hook；closed 狀態被 `_observe` 見到時才標記封存。在此之前候選資料
   原封保留且不會因 FK cascade 消失。若有人繞過正式 lifecycle，在兩次觀測之間將
   **同一** local/remote ID 關閉又重啟，且不留下 lifecycle generation，本模型無從辨識
   該未觀察狀態；正式 reopen 必須沿用「新帳戶／新 upstream 身分」契約。要支援該非正常
   路徑需後續獨立 lifecycle token，不應在此偷偷修改帳戶行為。

## 測試與驗證

- 本機 `python -m unittest discover -s tests -p 'test_llm_reconcile_*.py' -v`：
  **33 passed**（候選 adapter 12、balance 11、evidence normalization 10）。
- Odoo class 由 6 增至 **15 方法**；新增 9 項：pending 封存／不可變／不取樣、身分切換、
  已觀察 closure 後重啟、legacy unknown、封存失敗回滾、移除失敗回滾、精確 aggregate＋
  真 FK metadata、正常新帳戶 reopen 隔離、legacy backfill。
  **尚未執行真 Odoo／PostgreSQL 測試**，不與目前 pinned cf3b6dd1 的六項驗證混算。
- repo-lint --all：通過；C2 --all：1265 class 基準一致；compileall、git diff --check：通過。
  Runner SHA256 仍為 d757e3195a64eac11f2191d57008cd5999185a456ba87cd5cd525933012e9678。
- 原 `llm12b-i2-candidate-1021.md` 為 cf3b6dd1 的歷史報告；其中 closed discard／cascade
  描述已由本報告取代。來源 SHA 對應的歷史敘述不改寫。
