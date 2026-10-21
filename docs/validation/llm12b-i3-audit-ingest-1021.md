# I3 proxy-audit ingestion foundation（僅本機，未啟用）

Base `38cd584a39e72cad26834454c801224764d755fd`（I2 #1899 指定 head；I3 commit 已 rebase）；獨立 worktree
`impl/wt-llm12b-i3-audit-ingest-1021`，branch `feat/llm-12b-i3-audit-ingest-1021`。
未改 I2 或 Singer 工作樹、runner、cluster、aimodels／prod；未 push／PR。

## 精確範圍

- 純 `llm_audit_page.normalize_page` 驗證實際 controlstore.read_events v1 契約：
  environment、highwater、seq 連續、每頁恰好 min(100, head-after)、canonical UUIDv4、
  intent/outcome 欄位、型別與範圍、safe request ID、aware timestamp。
  bool／float 不可冒充整數；未知欄位、gap、短頁、重排、重複 attempt-kind、head 漂移皆拒絕。
  Timestamp 轉 UTC canonical text 保留 microseconds；不使用會截斷精度的 Odoo Datetime field。
- 新持久 `llm_audit_cursor`：每個明確 expected environment 一列，綁定既有設定指紋，保存
  after_seq／through_seq／snapshot_open／covered_seq。首次頁凍結 highwater；分批續頁固定 through。
  完整到 head 才推進 covered_seq；之後才開下一個 snapshot。這不是時間窗 coverage。
- 新 immutable `llm_audit_evidence` 保存最小安全事件，不存原始 JSON／headers／body／secret。
  UNIQUE(cursor,seq) 與 UNIQUE(cursor,attempt,kind)；exact replay 不重複插入，內容衝突拒絕覆寫。
  Intent 與 outcome 分列，絕不各算一筆 credit；孤立 intent 保留不確定性。
- 私有 `_ingest_tick(expected_env)`：先真 unconfigured gate（無 client／DB cursor／outbound），
  再要求 sudo；只有一頁、最多100事件、一個 CRON `control_audit()`。不註冊 cron、不接 UI/API。
  專屬 SQL cursor 的工作與 commit 共用30秒 absolute deadline；SQL lock budget 100ms，
  HTTP deadline 留1秒，只有自己的 stream row 鎖跨讀取，沒有 account/order 鎖。
- `_ingest_in_env` 是同一 transaction core，供 TransactionCase；production 入口固定使用
  `_ingest_tick` 的 deadline_cursor，不能拿 unbounded caller transaction 代替正式 tick。
  一頁的 evidence＋cursor 同 savepoint；exception／bad page／non-Ok(含 disabled 503)／插入失敗
  回滾全頁。上一頁已成功 commit 的進度保留，不為失敗頁前進。first-stream 競爭可能遇 UNIQUE／
  serialization；fail closed，交下一次呼叫重試。不吞 driver 或 transport exception，不印 raw error。
- Endpoint／identity 設定指紋變動拒絕繼續，不自動重設。System 僅 read ACL；CRUD 有 object
  capability，event immutable，cursor 不可任意刪除，evidence→cursor 是 RESTRICT，沒有 account FK。
  整個 module uninstall 仍可刪表；尚無 retention、bootstrap、配置修復或人工結案流程。

## 必須先補的契約／資料模型差異

1. `control_audit()` 以 CRON HMAC **簽請求**，HTTPS 驗 endpoint；現有 page／events 沒有獨立
   response signature、hash chain 或 store epoch。此切片不捏造簽章證明。expected_env 必須由
   受信任呼叫端先核實 binding，不能拿第一個回傳 env 當權威。設定指紋也不等於 source generation
   或 service-admin ownership 證明；本批不自動宣告所有 DB／tenant 歸屬有效。
2. Store restore／截斷造成 head 退後或 gap 會拒絕；同名、同 seq 的歷史換代若從未重新讀取舊 seq，
   現有 API 無從偵測。缺 epoch 的限制不能用 cursor 掩蓋。初始從 seq0 讀起；若早期事件已 prune，
   不允許跳過 gap，自動 bootstrap／retention recovery 另做可信契約。
3. Proxy 自己生成 UUIDv4 attempt_id，沒有由 Odoo 傳入；`llm_credit_order` 只有 attempt 計數與
   upstream request_id，未持久保存 proxy UUID。成功 forwarded 可能帶 safe upstream request_id，
   timeout/cancelled／孤立 intent 則無。不能只靠 target/quota/time 做無歧義訂單認領。
4. Control store 是 **add_quota intent/outcome 子集**：intent 在 AP admission 前寫入，可能尚未送出，
   outcome 也可能缺失；不含所有 stdout policy-denied/reveal/bg 稽核資料。不能稱為完整 proxy
   audit 或直接把 event 數等同 executed quota calls。AP type1/type4 log ingestion 尚未實作。
5. 尚未做 logical-attempt collapse、訂單認領、跨DB ownership、時窗完整性、I8 freshness、I2
   evidence integration、incident／recharge 或 GA acceptance。部署 control store／env binding、
   source retention與備份也不是本切片的一部分。

## 驗證

- 本機新純 parser：**10 passed**；既有 reconciliation 純回歸：**33 passed**。
- 新 Odoo TransactionCase：**9 方法，尚未執行**。涵蓋100＋1固定head續頁與 Environment reload、
  store unavailable／invalid rollback、部分snapshot gap／漂移、exact replay與衝突、第二列插入
  失敗回滾、unconfigured／expired零外呼、environment／設定漂移、CRUD／FK、>2^53存取精度。
  Responses 是合成 fixture；沒有真上游 HTTP，reload 不是已提交後 crash-restart 測試。
- C2 --all：1266 class 基準一致；repo-lint --all、compileall、git diff --check：通過。
  所有 Odoo schema install／upgrade、
  真 PG rollback／競爭／commit-time budget、stall HTTP 與重啟續頁實測仍待主代理隔離驗證。
- 本切片不是完整 I3、LLM-12b 或 GA 完成；不啟用自動分類／告警。
