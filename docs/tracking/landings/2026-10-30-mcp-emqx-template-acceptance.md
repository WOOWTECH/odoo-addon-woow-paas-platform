# EMQX MCP 模板來源驗收

新增正式 EMQX MCP 商品、獨立月／年方案及既有管理密碼啟動契約，固定 PR97 source `b5ed89ca01acfc33a7ef57987f0f64a11f603ea3`／manifest `sha256:f60858334f11147675a0071b5caf699692ccf31fb493347da5d856bf9a8828dc`。沿用 mcp-server chart0.1.9，不修改平台model、controller、schema、版本或其他MCP pins。租戶自行於Connection填broker API key／secret；39工具預設全開，SQL execute可獨立撤銷。

## 實際驗證

最終 listed source `b5e7b79925db41c3cb0eed4792400216a564e6e7` 在隔離真Odoo跑434個selected ORM方法：升級217、另一全新資料庫217，每次25classes、0 failures/errors/skips。完整1293檔source map與addon precedence已核對；28模板／160方案商品seed紀錄的fresh與upgrade快照相等。相對測試基底85a57df8，既有27模板、156紀錄及其XMLID metadata不變。此前hidden候選另有434方法通過，獨立保留，不合稱單次868測試。

獨立broker驗收來源由Mencius產生、Aristotle覆核：39工具列舉、實際retained CRUD／拒絕／SQL及兩次app PVC remount；本來源lane沒有重跑broker，也不聲稱39工具全部實際呼叫。local generator22、fixture guards7、listing guards5，以及repo-lint／collection基線通過。

05:51:26 UTC，UID限定的owned namespace、兩PV及Longhorn volume／replica物件已消失；本機watchdog身份也已退出。首個lease schema拒絕發生在資源建立前，收據保留。無production寫入、正式模板DB採用或artifact dispatch。

## 來源進位與限制

測試是在85a57df8基底上執行。隨後正常合入upstream PR2089／65795f19：僅HermesAgent chart0.4.4→0.4.5及對應測試／註解。source比對確認1291檔逐字不變、cloud XML恰為three-way merge、全部MCP records不變；這是來源reconciliation證據，不代表既有434方法測過Hermes0.4.5。版本仍沿用develop的18.0.1.0.280；.281由Main獨立release PR處理。

這是selected ORM測試，不是全套Odoo測試。全新依賴安裝有兩條既有 `res_company.sh_journal_id` NOT NULL schema訊息，在未改動基底同樣存在；不宣稱安裝log零ERROR，selected test runs則確為0 failure/error/skip。清理證據是物件不存在，不是磁碟位元安全抹除。

可機讀hash與外部原始收據定位在[驗收索引](../../verification/2026-10-30-mcp-emqx-template-acceptance.json)。Main負責PR review／merge與後續release；本lane沒有正式環境曝光動作。
