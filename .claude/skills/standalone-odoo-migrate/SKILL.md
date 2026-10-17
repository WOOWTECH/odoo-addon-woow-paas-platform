---
name: standalone-odoo-migrate
description: "把 standalone Odoo 站（odoo-tenant chart 收編的 *-odoo 站，例 harumi-odoo）搬進 PaaS 指定租戶的 Odoo 服務：盤點、相容性比對、addon 進 cloud-odoo image、還原、資料一致驗證、切換。"
when_to_use: 使用者要把 standalone／odoo-tenant 站搬進 PaaS、盤點某個 *-odoo 站能不能搬、比對站點 addon 與 cloud-odoo image，或要在沙盒起假 standalone 站測搬遷流程。
---

# standalone Odoo → PaaS 租戶搬遷

兩條硬需求（#1604）：**資料與來源逐位元一致**；**addon 由 paas-odoo-ci 烘進 cloud-odoo image**（不放 PVC）。
帳號與工作區由人決定、由人建立；服務由 agent 用網頁同一支 API 代購（下方「購買目標服務」），買之前先問清楚
（「開始前先問調用者」）。

## 分工

kubectl 白名單（CLAUDE.md 鐵律）決定誰跑哪一段：

| 段 | 執行者 | 交付物 |
|---|---|---|
| 來源 ns（standalone 站，不在白名單） | woow-machines-operator（WMO） | `scripts/source/` ＋ `scripts/odoo_inspect.py`、`scripts/addons_unpack.py`，連同指令交給 WMO；收回 JSON 與導出的 addon 目錄 |
| 目標（`dev-sandbox-*`、`paas-ws-stg-*`、`paas-ws-*`） | 本 repo agent | prod 寫入每一步都等人工放行 |
| 帳號、工作區、邀請操作帳號 | 人 | agent 不代建 |
| 購買與刪除服務（「購買目標服務」） | 本 repo agent，用工作區 owner／admin 的登入身分打網頁 API | prod 每次下單、刪除由人放行 |
| 平台 pod 的 odoo shell：停權、復權、Redeploy、conf 覆寫 | 人（平台管理員），用 `scripts/platform/cloud-service.sh`；agent 只給整行指令 | prod 平台寫入，agent 在 auto mode 下會被擋，不要繞過 |
| paas-odoo-ci PR merge、prod image `/approve` | 人 | agent 只開 PR |

## 追蹤 issue

**一站一張追蹤票，動手之前先開**（或確認已經有）：本 repo 的 Gitea issue，標題 `migrate: <來源 ns> 搬進 PaaS`，
body 寫 `Refs #1604`。範例：#1760（harumi；它是補開的，10-16 之前的紀錄留在 #1604 的留言、由 #1760 逐條連結）。

- **body＝現況**，每次有進展就改寫：來源（ns、hostname、tunnel）、正式目標（工作區、cloud_service id、release、ns）、
  方案與試用起始日、addon、conf 覆寫、寄收信、外部整合（MCP 等）、回切方式、證據與離線備份的位置；下面接待辦 checklist
  與「歷程」連結表。**留言＝歷程**，一個里程碑一則。
- work-state label 照 `docs/agents/triage-labels.md` 只掛一個：agent 在做 `ready-for-agent`、等人（驗收、WMO、放行）
  `ready-for-human`、等裁決 `needs-decision`；另掛 `derived`。來源下線、待辦全勾才關票。
- 什麼時候留言：

  | 時點 | 記什麼 |
  |---|---|
  | 「開始前先問調用者」每次有答案 | 已裁決表（項目、答案、日期）；答案改了就另起一則註明取代哪一則，不改舊留言 |
  | 步驟 1～3 | 盤點摘要（core_date、模組數、非 core addon、filestore、POS）、相容性比對的阻斷與注意事項 |
  | 步驟 5 | addon PR、image tag／digest、Sync 是否已按 |
  | 每輪彩排 | 彩排站 cs id、匯出量、restore／兩次 verify 結果、發現的缺口（缺口另開 follow-up 票） |
  | 購買目標服務 | cs id、release、ns、方案、`trial_end_date`；刪除也記 |
  | 切換前檢查 | 每一項的結論 |
  | 步驟 9 切換 | 切換紀錄：UTC 時間線表（時間｜動作｜結果）→ 技術驗收 → 回切方式 → 待辦（範例 #1604 留言 22602） |
  | 收尾 | 平台備份 id、離線備份位置、刪掉的資源、來源下線日期 |

- **不寫進 issue**：帳密、token、初始管理員密碼、客戶的 addon 原始碼、客戶資料內容（比對結果只列表名與筆數）。
- 本機證據（盤點 JSON、匯出 JSON、restore／verify log、DNS 備份）放 repo 旁的 `woow-<追蹤票號>-evidence/<set>/`，
  路徑寫進 body；它與 scratchpad 不同，不會被清掉。

## 開始前先問調用者

搬一個站有幾件事只有調用本 skill 的人能決定。照下表的時機逐項問清楚，答案寫進該站的追蹤 issue（見上一節）；沒有答案的項目
不要自己假設，也不要拿上一站的答案套用。

| 何時問 | 問什麼 | 為什麼要問 |
|---|---|---|
| 步驟 1 之前 | 來源站是哪一個 ns；這次做到哪裡（只盤點／彩排／正式切換） | 決定要請 WMO 做什麼、要不要排停機 |
| 步驟 1 之前 | 最終擁有新站的帳號與它的工作區（工作區由該帳號自己在網頁建）；服務名稱 | 帳單一律算在工作區 owner 名下，買了之後不能移轉（見「購買目標服務」） |
| 步驟 1 之前 | 彩排站買在哪個工作區（通常是調用者自己的） | 彩排站放的是真實客戶資料；刪掉之後 PVC 仍保留（prod 90 天），那個工作區的 owner／admin 取得到 |
| 步驟 1 之前 | 兩個工作區各用哪個登入身分下單：工作區 owner 的帳密，或 owner 邀請一個操作帳號當 admin；兩台各自的管理員登入 Email | 下單 API 只認登入 session，沒有 token 可用；帳密私下交接，不經 issue、PR 或 log |
| 步驟 3 之後 | 方案大小與繳費週期——附上來源的 workers／記憶體與各方案規格，給出建議；彩排站用同一個方案 | workers、記憶體與磁碟大小由方案決定，不能逐台改；試用中換方案會立刻結束試用並扣款，下單前要定案 |
| 步驟 3 之後 | 寄收信主機是叢集內的共用服務（mailcow）時，同不同意把目標 ns 接上 | 准入 label 是整個 ns 的 |
| 彩排通過後 | 要不要沿用來源的 hostname | 沿用要請來源 DNS 的管理者（WMO）在切換當下刪舊記錄，要先約好人 |
| 彩排通過後 | 外部整合（MCP、webhook）要不要改指、由誰改 | 它們連的是來源的內部位址 |
| 彩排通過後 | 切換時段；誰通知使用者、誰確認 POS 已關帳、誰驗收 | 停機與驗收都要有人在場 |
| 彩排通過後 | 回切期限（幾點前驗收不過就回切）；來源保留多久才下線 | 見下方「備份與失敗還原」 |

排切換日之前，把「備份與失敗還原」整節向調用者說明一次並取得確認：哪些東西有副本、每個階段失敗怎麼回復、哪一刻
之後不能回切。調用者沒確認就不進步驟 9。

## 購買目標服務

每一站在 prod 買兩台，都由 agent 代購：

| 服務 | 買在誰的工作區 | 何時買 | 何時刪 |
|---|---|---|---|
| 彩排站 | 調用者指定的工作區（通常是調用者自己的） | 範本的 image 已換成步驟 5 那顆、步驟 2 重比 `READY`，來源用到的 addon 都已出現在 `/api/cloud/odoo-addons` 之後 | 彩排做完、下次付款日之前 |
| 正式目標 | 最終擁有帳號的工作區 | 彩排通過、切換日前幾天（留時間做「切換前檢查」的設定） | 不刪 |

- 服務的 image 在下單當下取自範本預設值，租戶不能指定。新 addon 要等平台 image 上線、平台管理員按「Sync Odoo Addons」
  （`POST /api/cloud/odoo-addons/sync`，限平台管理員）才會出現在清單裡。
- 彩排拖過試用期：在下次付款日之前刪掉、重買一台（每台新服務各有一次試用）。重買一次就多寄一封確認信、多一組保留中的
  PVC 吃儲存配額，不要反覆重買。
- 正式目標下單後就在跑試用：切換改期超過試用期會向 owner 扣第一期；停權不會停止計費。改期時先告訴調用者。

**不要先買在自己名下、驗收後再移轉。** 平台沒有可用的移轉：工作區 owner 的移轉方法沒有接到任何畫面或 API、本身也走不完
（#735 未裁決）；服務不能換工作區（namespace 與網址由工作區決定）；訂閱的付款人在下單當下就固定成當時的工作區 owner，
之後換 owner 也不會跟著改。

**怎麼買**：用工作區 owner 或 admin 的登入 session（`POST /web/session/authenticate` 取 cookie；開了 2FA 的帳號先確認
登得進去）打網頁用的同一組 API（`auth="user"`、JSON-RPC），紀錄才會與網頁相同。admin 要先由 owner 邀請、受邀者接受。

1. `POST /api/workspaces`（`action: list`）找到工作區 id；`POST /api/workspaces/<id>/services`（`action: list`）看有沒有
   同名的服務。
2. `POST /api/cloud/templates` 找 slug `odoo` 的範本 id；`POST /api/cloud-services/templates/<範本 id>/plans` 取方案，
   只選它回傳的（隱藏方案不在裡面；後端不擋，要自己守），看 `has_trial`／`trial_duration`／`trial_unit` 確認有試用。
   `POST /api/cloud/odoo-addons`（params `template_id: <範本 id>`，與網頁相同）取 addon，用 `technical_name` 對來源
   的模組；缺任何一個就停，回到步驟 5 的 Sync。
3. `POST /api/workspaces/<id>/services`，params：`action: create`、`template_id`、`name`（2～100 字，網頁只在前端
   檢查，代購要自己守）、`plan_id`、`odoo_addon_ids`、`values: {"conf.adminEmail": <Email>, "loadDemoData": false}`。
   Email 要用收得到信的信箱（`example.com` 之類的保留網域會被拒）。
4. `POST /api/workspaces/<id>/services/<服務 id>`（`action: get`）確認 `state` 是 `running`、`subscription.is_trial`
   為 true，記下 `trial_end_date`，結果記進追蹤 issue。下單回 success 只代表紀錄建好；儲存配額或叢集儲存（Longhorn）
   不夠時，之後開通才被拒：服務轉 `error`、訂閱自動取消、不扣款，服務要自己刪掉。

- 回 `{success: false, error}` 時整筆沒有落地，照錯誤修正後再送。只有逾時或連線中斷、不知道結果時，先回到第 1 步
  list：平台不擋重複下單，重送一次就多一台服務、多一筆訂閱、多一封信。
- 不要在平台 pod 用 odoo shell 自己建（就算帶 `_internal_provision` 開閘也一樣）：方案檢查、values 白名單、初始密碼、
  方案規格 overlay、建訂閱、觸發部署都在 controller 的 `_create_service`，繞過它紀錄就和網頁不同。
- 用 admin 身分下單時，帳單仍算在 owner 名下；服務與訂閱的 `create_uid` 是操作帳號，服務頁上平台寫的錯誤說明會用它的
  語言與時區。工作完成後請 owner 移除這個 admin（兩個工作區都要）。
- 下單當下平台就寄訂閱確認信給 owner，撤不回——先告訴 owner。
- 初始管理員密碼只在 create 回應的 `data.admin_password` 出現這一次（帳號是 `data.admin_username`）。交給調用者時
  不經 issue、PR 或 log；還原後帳號換成來源站的，這組就不再適用。
- prod 上每次下單、刪除都是寫入，逐次由人放行。

**試用與儲值**：每台新服務各有一次試用，長度看 plans 回應的 `trial_duration`（目前 Odoo 方案是 7 天）。下次付款日＝
下單日＋試用天數，那天的每日排程就會扣款。下單時不檢查錢包，也不預檢 CPU／記憶體配額。

- 付款日從 owner 的錢包扣第一期，不夠時用綁定的卡自動補差額。扣不到就每天重試，服務照常運作；低餘額提醒信每期只寄
  一封、只寄給 owner。連續失敗 6 次（預設值；約下單後第 12 天）訂閱取消，當天或隔天停權；取消之後補繳舊帳也不會復權，
  要重新續訂（只能用錢包付）。
- 所以下單時就提醒 owner：付款日之前錢包至少要有一期的錢（例：1–3 人月繳 1500 點），或先綁卡。付款日前 3 天平台會依
  錢包餘額寄「請儲值」信，已綁卡也會收到。不要按「略過試用」：它立刻結束試用並扣款，而且只扣錢包。正式目標的觀察期
  一定超過試用期，這一步不能省。
- 開通停在 `error`、從沒 running 過的服務不會被扣款，但訂閱仍在進行中，要把服務刪掉。

**刪除彩排站**：順序是 `verify.sh --stage upgrade --cleanup`（刪掉含客戶 dump 的 incoming PVC）→ 用網頁同一支
`POST /api/workspaces/<id>/services/<服務 id>`（`action: delete`）刪服務 → 刪 ns 裡的 `standalone-odoo-rehearsal-login`
Secret。彩排站與正式目標在同一個工作區（同一個 ns）時，這三步都要在正式切換的 `receive.sh open` 之前做完——incoming PVC、
receiver 與 Secret 都是 ns 內的固定名稱，`receive.sh open` 會沿用已存在的 incoming PVC。

- 試用期內刪除沒有扣過款，也就沒有退點。
- 刪服務之後，資料、PostgreSQL、備份三顆 PVC 仍保留（prod 90 天），期間照算那個工作區的儲存配額（每個工作區 ns 最多
  10 顆 PVC，一台 Odoo 用 3 顆），該工作區的 owner／admin 也能取回裡面的資料。要提早清，請平台管理員處理。

## 步驟

每支腳本的參數與 exit code 寫在檔頭（`sed -n '2,20p' <script>`），這裡不重複。
開始前先確認這一站有追蹤 issue（上方「追蹤 issue」）；每一步的完成條件達成後，把結果留言到追蹤 issue，並更新 body。

1. **盤點來源**（WMO）：`scripts/source/inventory.sh --context woow-k3s --ns <ns>`。
   完成條件：拿到 `inventory-<ns>-<UTC>.json`，stderr 摘要列出 core_date、模組數、非 core addon、filestore。
2. **盤點目標 image**（agent）：`scripts/target/image-inventory.sh`，`--image` 用目標範本的 cloud-odoo digest
   （`src/data/cloud_app_templates.xml` 範本 4 的 `helm_default_values.image`），`--ns` 用有 `paas-registry`
   pull secret 的白名單 ns。完成條件：拿到 `image-inventory-<digest>.json`。
3. **相容性比對**（agent）：`scripts/compat_check.py --inventory … --target … --ci-repo <paas-odoo-ci checkout> --json …`。
   完成條件：報告存檔，且依結論處理——
   - `READY`（exit 0）：進下一階段。
   - `ACTIONS`（exit 1）：每個 `NEED_IMPORT`／`NEED_LIST`／pip 缺口都變成 paas-odoo-ci PR 的一行；`DIVERGED`
     與注意事項逐條列給使用者裁決。新 image 建好後回到步驟 2 重比，直到 `READY`。
   - `BLOCKED`（exit 3）：停下，把阻斷原因原文回報使用者。
4. **資料指紋**（凍結窗口內，WMO）：`scripts/source/fingerprint.sh`，產出 `.fp`（每張表筆數＋內容 hash、sequence）
   與 `.fs`（filestore 逐檔 sha256），跟 dump 一起交回。目標還原後在目標端產同格式的兩個檔，用
   `scripts/fingerprint_diff.py --stage restore` 比對；升級閘跑完 `-u all` 後再以 `--stage upgrade` 比一次。
   完成條件：`restore` 階段 exit 0（逐位元一致）；`upgrade` 階段 exit 0，且「預期內的變動」逐條看過。
5. **addon 進 image**（WMO 導出、agent 開 PR、人 merge 與 `/approve`）：
   - 導出原始碼（WMO）：`scripts/source/export-addons.sh --inventory <步驟 1 的盤點檔> --out-dir <新目錄>`。唯讀 exec，
     逐模組對盤點檔驗內容 hash；完成條件：exit 0，拿到 `<out-dir>/addons/` 與 `addons-export.json`。原始碼是客戶的
     程式碼，留在本機交接，不貼進 issue／PR 留言。
   - paas-odoo-ci PR（agent）：以步驟 3 的報告為準——`NEED_IMPORT` 的模組從 `<out-dir>/addons/` 原樣複製進
     **`addons-cloud/`**（只進 cloud-odoo、不進平台 image；不要放 `addons/`，也不要列進 `config/paas-addons.list`），
     `NEED_IMPORT`＋`NEED_LIST` 加進 `config/cloud-addons.list`，重跑 `generate-catalog.sh`／`generate-requirements.sh`
     （來源目錄 `addons:addons-cloud`）更新 `addon-catalog.json`、`requirements-cloud.txt`。不改 addon 的任何檔，
     git tree 的內容 hash 才會與來源相同。
     - `DIVERGED`：同名模組不能同時在 `addons/` 與 `addons-cloud/`，只能「用 paas-odoo-ci／目標那份」或「把那份換成
       來源的」。逐檔 diff（先 `--strip-trailing-cr`，差異常常只是 CRLF）後寫進 PR 描述給人裁決；裁決用現有那份的，
       之後的比對加 `--accept-diverged <模組,…>`。
     - **多站共用、各站程式碼不同的模組**（例：LINE 三件組在 9～10 站有 3～5 種程式碼）：一顆 image 同名模組只放一份，
       上面「原樣複製」只適用於只有一站有原始碼的模組。這類模組先合併成一份再進 paas-odoo-ci，搬遷前先確認：
       - 版本號要 ≥ 所有要搬的站的 DB 版本，否則那一站比對會判 `BLOCK_DOWNGRADE`。合併版在每一站都會是 `DIVERGED`；
         裁決用合併版之後，每一站的比對都要帶 `--accept-diverged`，列了哪些模組記進該站的追蹤 issue。
       - 合併版的升級（migration script、改了 stored compute 欄位的算法）若會改到既有的客戶資料列，步驟 8 的 upgrade 比對
         一定不通過，現在也沒有放行機制。合併時避開；避不開就先改比對（`fingerprint_business.sql`／`fingerprint_diff.py`），
         不要帶著差異上線。每一站都要先在彩排把 `-u all` 走過一次。
       - 平台 Odoo 自己也裝的模組放在 `addons/`，平台 image 也帶。合併版放進去，平台 image 跟著換版，平台要另排一次
         `-u <模組>` 與測試（#1820）；PR 描述要列出受影響的平台模組。只給租戶用的才放 `addons-cloud/`。
       - 已在 PaaS 的租戶（含先前搬進來的站）不會馬上換：服務的 image 釘在自己的 `helm_values`，下次 Redeploy 才換成
         範本的新 image、跑 `-u all`。範本換 digest 時列出受影響的服務，事先通知。
     - pip：`requirements-cloud.txt` 由 CI 從清單內模組 manifest 的 `external_dependencies` 產生。manifest 寫的是
       import 名而 pip 裝不了時（例 `PIL`），在 paas-odoo-ci 的 `generate-requirements.sh` 對照表補一筆，不改 manifest。
     - 匯入前掃一次寫死的密碼／金鑰；`addons-export.json` 裡 `symlinks_outside` 非空的模組先處理連結。
     - `cloud-addons.list` 一動，平台 image 也要重 build（catalog 在平台 image 裡，平台才看得到新 addon）。
   - build 出新 cloud-odoo 後，目標服務改用這顆 image，回到步驟 2 重比到 `READY`。data_dir 的 addon 也要進 image：
     升級閘只讀 image 的 `addons_path`，不讀 data_dir。完成條件：目標 image 的盤點涵蓋來源所有已安裝模組。
6. **搬運**：目標端 `scripts/target/receive.sh open`（agent）印出 `--url` 並寫出 token 檔 → 兩者交給 WMO 跑
   `scripts/source/export.sh` → `receive.sh close`。export.sh **只檢查、不凍結**：預設要求來源 odoo 已 scale 0、沒有 pod
   （否則 `ERROR: … is not frozen`），凍結指令見步驟 9.2；`--live` 跳過檢查，只給彩排。
   完成條件：`export-<ns>-<UTC>.json` 每個檔的 sha256 都跟 receiver 回報一致（export.sh 失敗即 exit 3）。
   - helper pod 剛起來時，來源 ns 與目標 ns 的 NetworkPolicy 還沒放行它的 IP，連 PG、連 receiver 都會被拒（harumi
     prod 彩排與沙盒實測 Connection refused）。export 會先等 receiver 的 `/healthz`、連 PG 失敗也會重試，各最多約 60 秒；
     stderr 的 `receiver retry`／`connect retry` 行就是在等這個。
7. **還原**（agent）：平台停權 → postgres 開回 1 → `scripts/target/restore.sh --replace-db` → `scripts/target/verify.sh --stage restore`。
   - 停權走平台（見下方「平台服務的停權與復權」）：web 與 `<release>-postgresql` 都會縮到 0、對外網址改指停權說明頁。
     restore Job 要連 PG ⇒ 停權後 `kubectl scale sts/<release>-postgresql --replicas=1` 並等它 Ready。
   - restore Job 內已逐位元比對，一致才留下客戶資料列基準（`baseline.bfp`）並預寫兩個標記：`upgrade_done`（來源 image
     digest＋core_date，讓 web 開機時升級閘自動跑 `-u all`）與 `tenant_localized`（擋掉平台的台灣在地化，見步驟 8）。
   - **彩排**（來源還在營運，目標是彩排站）一律加 `--neutralize`，見下方「彩排與中和」。正式切換不加。
   - 寫完標記後 restore 會**暫停排程**到 N 分鐘後（`--cron-hold-minutes`，預設 120，0＝不暫停）：啟用中、`nextcall` 早於那個
     時點的排程與 trigger 一律延到那個時點，原值存在 `ir_config_parameter` 的 `standalone_odoo_migrate.cron_hold`；另在
     `ir_cron`／`ir_cron_trigger` 掛 PG trigger，暫停期間才排的 trigger、被改寫或重新啟用的排程也夾到那個時點。理由：
     web 一開機，到期的排程就跑、改到客戶資料，升級後的比對就不準。到期的來源除了還原進來的過期 `nextcall`，還有 Odoo core
     每次更新模組（`-u all` 結束時，`odoo/modules/loading.py`）都會排的 autovacuum trigger（1 分鐘後）——harumi prod 彩排第二輪
     實測它刪了 2 筆已關帳 POS session 的 `ir_sequence`（第一輪也是 autovacuum 刪了同樣 2 筆）。時間到了沒人放開，PG trigger
     自己失效、排程照常恢復。
   完成條件：Job 印出 `RESTORE-DONE`（暫停排程時多一行 `crons held until …`），verify exit 0。
8. **升級驗證**（agent）：平台復權，等升級閘跑完、pod Ready → `scripts/target/verify.sh --stage upgrade` → Redeploy
   （`cloud-service.sh redeploy`，等 `status` 回到 running）→
   再跑一次 `verify.sh --stage upgrade --cleanup`（比對通過才放開排程、刪 restore Job 與 incoming PVC；彩排與正式切換
   都一樣）。這次就是最後一次比對：排程一放開，收信、金流等排程就會改到客戶資料列（harumi 正式切換實測：fetchmail
   一跑就更新 `fetchmail_server` 那一列），之後再比一定不通過。想先留著 incoming PVC 再查時改帶 `--release-crons`，
   事後用步驟 9.7 的手動指令刪。
   initContainer exit 4＝image 缺來源模組（DB 沒被動），回步驟 5；exit 78＝DB core 比 image 新，image 選錯。
   判準是客戶資料列（`scripts/fingerprint_business.sql` 檔頭）：還原當時就存在的客戶資料列必須原封不動；模組資料列
   被 `-u all` 重寫、升級後新增的列、registry 表的變動列為「預期內」。模組擁有的列（含 noupdate）的可翻譯欄位只比
   en_US——升級會替它們補上新翻譯。filestore：仍被附件引用的檔不能少、內容不能變；
   模組附件換了內容後被 Odoo 回收的舊檔列為「預期內」。
   完成條件：兩次 verify 都 exit 0，「預期內的變動」逐條看過；最後一次印出「排程已放開」與 `incoming PVC … deleted`。
   - 排程要等最後一次比對通過才放開：放開後過期的排程跑一輪、改到客戶資料，之後再比就不準。比對沒過、但差異查明可以接受
     （或要提早恢復排程）時，用 `scripts/target/cron-hold.sh status|release` 手動處理。
   - 放開（`cron-release.sql`）先移除 PG trigger，再處理仍停在暫停時點的排程與 trigger：暫停當時就有的還原原值，暫停期間
     才夾住的（開機途中排的 trigger、`-u all` 改寫或重新啟用的排程）改成現在——兩種都在一分鐘內跑一輪。`-u all` 依 XML 把
     `nextcall` 改到暫停時點之後的（沙盒實測 Digest Emails）不動。
   - upgrade 階段的 verify 取完指紋後會檢查暫停是否擋住：暫停起點之後有排程跑過（`lastcall`）或正在跑（cron job 對
     `ir_cron` 持有鎖）就判不通過——指紋之後還可能再跑，比對通過也不可信（彩排第二輪舊版暫停就是這樣「剛好通過」）。
   比對要在任何人登入目標站之前跑：管理員登入會改寫 `web.base.url`，之後的比對會多出 `ir_config_parameter` 不一致。
   對網址的任何 HTTP 請求（含 agent 自己的 `curl /web/login`）會新增 `website_visitor`／`website_track` 列，列為「升級後新增」不擋。
   session 檔不在搬遷範圍：搬過去之後所有人都要重新登入（Odoo 的 autovacuum 會把舊的裝置紀錄標成已撤銷）。
   - 平台的 Redeploy 與改 addon 會觸發部署後初始化（operator `POST /init/odoo`：更新模組清單＋台灣在地化）。在地化會裝
     zh_TW、改管理員語言／時區與公司國家／幣別；restore 預寫的 `tenant_localized` 讓它整段跳過。所以第一次 verify 在 Redeploy
     之前跑、不帶旗標，Redeploy 後的那一次才放開排程（或 `--cleanup`）。
   - 匯入後管理員帳密是來源站的，平台存的那組不再適用；租戶可在服務頁按 Reset 重設（重設 `base.user_admin`、清掉 2FA）。
   ⚠ 排程放開（或暫停時間到）之後就開始跑（寄信、收信、金流、外部整合）。來源沒凍結的彩排（`export.sh --live`）不中和的話，
   兩邊會同時對外動作 ⇒ 彩排的還原一定要 `--neutralize`；暫停排程只是為了比對，不能代替中和。
9. **cutover**（正式切換；prod 每一步人工放行）。前置：彩排通過、正式目標已照「購買目標服務」買在最終擁有帳號的
   工作區、下方「切換前檢查」逐項有結論。停機時間＝凍結匯出＋還原＋開機升級＋比對（harumi 的量：約 15 秒＋45 秒＋3 分鐘，另加人工步驟）。
   1. 正式目標先調好設定（「切換前檢查」的 conf 覆寫、需要時 `shared-mail.sh open`），Redeploy 完成、服務 running。
   2. 凍結來源（WMO）：公告停機 → POS 全部關帳、沒有 draft 訂單 → 來源 odoo scale 0（postgres 留著）。
      - 關帳＝每台收銀機在 POS 畫面按 Close Register 做完結班（點現金、過帳），`pos.session` 的 state 都是 `closed`。
        POS 會把訂單先存在平板的瀏覽器再同步；session 開著就凍結，平板上還沒同步的訂單不會在匯出的資料裡。
      - scale 0 之前重跑一次 `inventory.sh`（唯讀），摘要的 `pos` 那行是 `open sessions 0, draft orders 0` 才往下。
        後面若多一段 `idle opening_control N (…)`：那些 session 停在開帳步驟（`opening_control`、沒有 `start_at`、0 訂單）——
        有人打開 POS 但沒按開帳，前端停在開帳視窗不能點單，平板上不會有未同步的訂單，**不擋凍結**；照原狀搬，店家在新站
        接著開帳（harumi 的 session 185 就是這樣，10-10 到 10-16 一直掛著）。
      - 凍結與回切的指令（來源 ns 不在本 repo 白名單，由 WMO 或人跑；`<odoo>` 是來源的 odoo Deployment）：
        `kubectl --context <ctx> -n <ns> scale deploy/<odoo> --replicas=0`，再 `kubectl get pods` 確認 `<odoo>-*` 都消失才跑
        export；回切＝`--replicas=1`。export.sh 不會替你 scale。
   3. 走步驟 6～8：`export.sh` **不帶** `--live`、`restore.sh` **不帶** `--neutralize`。兩階段 verify 都 exit 0 才往下；
      不通過就回切（第 6 點），不要帶著差異上線。比對在任何人登入之前跑完。Redeploy 後那次 verify 帶 `--cleanup`
      （放開排程並刪 incoming PVC）：排程沒放開的話，第 5 點驗收的收發信不會動（最晚到暫停時間結束才會自己恢復）。
   4. 對外網址改指（下方「對外網址」的三步順序；`curl` 回到目標站才算完成）、外部整合改指（MCP、webhook 來源端的設定）。
   5. 驗收（人）：登入、開一個 POS session、收發各一封信、金流測一筆。通過後立刻手動加一份平台備份
      （「備份與失敗還原」），再開放給使用者。
   6. 回切：新站產生第一筆真實交易之前可以回切——匯出全程唯讀，來源資料沒被動過；之後就不能回切（兩邊資料分岔）。
      各階段的回復動作與順序見「備份與失敗還原」。要不要回切由調用者決定，agent 不自行決定。
   7. 收尾：incoming PVC 已在第 3 點的 `--cleanup` 刪掉；來源保留到約定的觀察期滿再下線（WMO）。
      第 3 點改帶 `--release-crons`、PVC 還留著時，**不要**再跑 `verify --cleanup`（排程放開後必定不通過、不會刪），直接刪：
      `kubectl --context <ctx> -n <target-ns> delete job -l app.kubernetes.io/part-of=standalone-odoo-migrate,app.kubernetes.io/component=restore`
      → `kubectl --context <ctx> -n <target-ns> delete pvc standalone-odoo-incoming`（先刪 Job：它的 pod 還掛著這顆 PVC）。

## 切換前檢查

每一站在彩排後、排切換日之前逐項確認；`compat_check.py` 的「注意事項」會列出其中幾項。

- **方案與資源**：目標的 `workers` 與記憶體上限由購買的方案決定（`cs_odoo_workers`／`cs_ram_gb`），不能逐台覆寫。
  來源的 `workers`、`limit_memory_*` 用來選方案；選小了，切過去會變慢或 worker 被記憶體上限中止。
- **conf 覆寫**：`limit_time_cpu`／`limit_time_real`／`server_wide_modules` 不在方案內，chart 預設是 60／120／`base,web`。
  彩排站與正式目標都要套（彩排才驗得到 `server_wide_modules` 能不能開機）。平台管理員跑
  `scripts/platform/cloud-service.sh conf … --limit-time-cpu <秒> --limit-time-real <秒> --server-wide-modules <csv>`：
  寫進服務 `helm_values` 的 `conf.*` 後 Redeploy，再用 `status` 等它回到 running、看 `conf` 三個鍵
  （stg 實測：odoo.conf 生效、`server_wide_modules` 帶 `/all-addons` 的模組可以開機；harumi 用 1200／2400／
  `base,web,mail_fetchmail_durable`）。
- **寄收信主機**：盤點的 `db.mail_hosts`。conf 層的 SMTP 不會帶過去（改用 DB 的 `ir.mail_server`）。叢集內位址
  （`*.svc.cluster.local`）從租戶 ns **連不到**——baseline egress 只放行同 ns、DNS 與對外網路。
  - 指向 **mailcow**（woow-k3s 的共用 mail 服務，不在 PaaS 內，歸 woow-machines-operator 管；只有叢集內位址）：
    正式目標買好、running 之後跑 `scripts/target/shared-mail.sh open`（彩排站不接）——目標 ns 加准入 label、加一條只開給這台 Odoo
    的 egress 規則，並實測連線。每一站接上之前先取得使用者同意（label 是整個 ns 的，同工作區的其他服務在 mailcow
    那一側也會被放行）。stg 實測：接上後 587／993 連得到、同 ns 其他 pod 仍被擋、Redeploy 後仍在、`close` 可收回。
  - mailcow 用私有 CA：收信靠 `mail_fetchmail_durable` 內附的 `mail-ca.crt` 驗憑證（stg 實測可驗）⇒ 該模組要在
    `conf.serverWideModules` 裡。
  - 其他叢集內主機沒有現成作法，要先與該服務的負責人談。
- **對外網址**：要不要沿用來源的 hostname（印在桌上的 QR code、金流回呼、外部系統都指向它）。見下方。
- **外部整合**：連到來源內部位址的東西（MCP server、其他服務的 webhook）切換後要改指新服務。
- **登入**：session 不搬，所有人要重新登入；平台開通時給的管理員帳密不適用，帳密是來源站的。
- **金流與發票**：正式切換不中和，來源的金流供應商、發票憑證原樣帶過去，回呼網址跟著對外網址走。

## 備份與失敗還原

排切換日之前向調用者說明本節並取得確認。

**資料的副本在哪裡**

| 副本 | 位置 | 留到什麼時候 |
|---|---|---|
| 來源站本身 | 來源 ns 的 PVC。匯出全程唯讀（helper pod 的掛載都是唯讀）；凍結只是把 odoo scale 0，postgres 與資料都留著 | 約定的觀察期滿，WMO 才下線 |
| 匯出組（DB dump、filestore tar、指紋） | 目標 ns 的 incoming PVC；每個檔的 sha256 記在 `export-<ns>-<UTC>.json` | `verify.sh --stage upgrade --cleanup` 刪除 |
| 新站的平台備份 | 目標服務自己的 backup PVC（與資料不同 volume、同一座叢集）。`<release>-backup` CronJob 每天一份：DB dump＋filestore＋manifest，保留 7 份每日＋4 份每週 | 隨服務存在 |

- 驗收通過後立刻手動加一份，不等當晚的排程：`kubectl -n <ns> create job <名稱> --from=cronjob/<release>-backup`，
  Job log 出現 `Verified backup` 才算數（stg 實測可用）。prod 上這是寫入，由人放行。
- 平台目前沒有給租戶按的備份／還原按鈕。要把新站從平台備份還原，由平台管理員照
  `docs/guides/deployment/tenant-odoo-restore-runbook.md` 人工處理（該檔的租戶段還沒寫完）。還原只回到備份的時點，
  之後的資料丟失；已寄出的信、金流與發票序號不會跟著回去。
- 來源 PVC 可以請 WMO 在凍結後加拍一份 Longhorn snapshot 當還原錨點。它與資料在同一顆 volume，不算備份。

**每個階段失敗怎麼回復**

| 失敗在哪裡 | 來源狀態 | 回復 | 資料損失 |
|---|---|---|---|
| 步驟 1～5、彩排 | 照常營運 | 不必回復；彩排站可以整個重來 | 無 |
| 凍結之後、網址改指之前：匯出失敗、還原或比對不過、升級閘 exit 4／78、升級後比對不過 | odoo scale 0 | 來源 odoo scale 回 1（WMO）就恢復營運，網址沒動過。目標站停權留著查原因，修好後 `restore.sh --replace-db` 可重跑，另排一次切換 | 無 |
| 網址改指之後、新站還沒有真實交易 | odoo scale 0 | 依序：新站停權（讓排程停下來）→ 刪新 route、DNS 指回來源 tunnel（WMO）→ 來源 odoo scale 回 1 → 外部整合指回來源 | 驗收時在新站做的測試資料 |
| 新站已有真實交易 | 不再使用 | **不能回切**（兩邊資料已分岔）。在新站修；最壞情況從新站的平台備份還原 | 還原會丟掉備份時點之後的資料 |

- 「真實交易」＝新站上不能重做的事：訂單、付款、開出的發票、收進來或寄出去的信。
- 回切期限事先講好（例：預定開店前 30 分鐘驗收還沒過就回切）。
- 回切之後要再切一次，得重新凍結匯出：來源恢復營運後資料又變了，舊的匯出組不能再用。
- 新站一開機排程就會收信、寄信。依 Odoo 的行為（未實測）：新站收過的信來源不會再收到，凍結當時還在佇列的信回切後
  來源會再寄一次——回切時把這兩件事告訴調用者。

## 對外網址

平台服務自帶一個 `<subdomain>.<平台網域>` 網址。要讓來源的 hostname 繼續指到新服務，用 operator 的 route API 另加一條：

- `POST /api/routes`，body `{"subdomain": "<來源 hostname 的第一段>", "service_url": "http://<release>.<ns>.svc.cluster.local:80"}`
  ——在平台 tunnel 加 ingress rule 並建 DNS CNAME。只適用於與平台同一個網域的 hostname。
- ⚠ **API 回 201 不代表網址已經改指**。那筆 DNS 記錄還指向來源那條活著的 tunnel 時，operator 只加 ingress rule、
  不動 DNS（log 一行 `Refusing to repoint DNS record …`），照樣回 201，流量繼續走來源。完成條件一律是
  `curl https://<hostname>/web/login` 回到目標站的登入頁，不是 API 的回應。
- 順序（stg 實測，2026-10-13，測試 hostname 先指向一條 standalone tunnel）：
  1. 切換前先呼叫一次 `POST /api/routes`：ingress rule 先放好，DNS 被拒，對使用者沒有影響。
  2. 來源凍結、還原與比對通過後，請來源 DNS 的管理者（WMO）刪掉那筆 CNAME——一個 API 呼叫。此刻起 hostname 解析不到。
  3. 再呼叫一次 `POST /api/routes`：這次 operator 會建 DNS 記錄。實測約 20 秒後測試網址回到目標站。
  網址中斷的長度＝第 2 步到第 3 步生效；事先與 WMO 約好，刪完立刻通知。
- 回切：`DELETE /api/routes/<subdomain>`（移除 ingress rule 與 operator 自己建的 DNS 記錄）→ WMO 把 CNAME 建回來源 tunnel。
  所以來源 tunnel、它的 ingress rule 與 cloudflared 要留到觀察期滿才清。DNS 記錄不屬於平台 tunnel 時，`DELETE` 只移除
  ingress rule、不碰那筆記錄（實測）——第 1 步放錯了可以直接撤掉。
- 每個 standalone 站各有一條 tunnel（WMO 查證；個別站一條 tunnel 掛多個 hostname）。刪 CNAME 只影響那一個 hostname。
- 額外 hostname 指到彩排站的 stg 實測：加上後可用；Redeploy 後仍在；**停權時它回 502**，不會像服務自己的網址改指停權說明頁。
- 平台不認得這條 route：刪服務時不會一起刪，要手動移除。Odoo 端的 `web.base.url` 是來源帶過來的原值，不必改。
- prod 的 route 是 prod 寫入，由人放行後執行。

## 彩排與中和

彩排＝來源站照常營運，用 `export.sh --live` 取一份資料到彩排站走完步驟 6～8。彩排站拿到的是真實資料與真實的對外設定，
開機前必須中和：`restore.sh --replace-db --neutralize`。彩排站另外買、試用期內刪（「購買目標服務」）；它不接 mailcow、
不加 route，不要拿正式目標當彩排站。

- **時機**：還原並與來源逐位元比對通過之後、取客戶資料列基準之前。所以 `verify.sh --stage restore` 證明的仍是「搬過來的
  資料與來源相同」，`--stage upgrade` 證明「中和後的資料沒被升級動到」，並重跑一次中和斷言。
- **官方 SQL**：Job 用 release 的 odoo image 跑 `odoo neutralize --stdout`，依已安裝模組組出——停用排程（autovacuum 除外）、
  寄信主機換成假的、停用收信、金流供應商、webhook、網站網域等。
- **補充 SQL**（`scripts/target/neutralize-supplement.sql`）：全部使用者的密碼換成彩排密碼、刪 API key 與 2FA；排程的
  模組資料改 `noupdate`（`-u all` 不會把它開回來）；金流供應商全部停用（含測試模式，否則 `payment` 模組升級時會把
  交易後處理排程開回來）；WoowTech addon 的對外整合（綠界發票換測試商店、綠界金流憑證、
  POS 自助點餐的 Uber Direct 與出單代理）。
- **斷言**（`scripts/target/neutralize-assert.sql`）：有任何一項沒中和（`nfail` 行）Job 就失敗、不寫 marker，Odoo 開不了機。
- **要人看的清單**：Job log 與 `neutralize-report.txt` 的 `nscan` 行＝欄名像憑證、仍有值的欄位（只列表.欄與筆數）。
  逐條判斷是不是對外憑證；是的話寫一段 SQL，用 `--extra-neutralize-sql <file>` 重跑還原。完成條件：`nscan` 逐條看過。
- **登入**：彩排密碼在 Secret `standalone-odoo-rehearsal-login`（key `password`），所有原本有密碼的帳號共用；帳號名稱不變。
  畫面上方會有 Odoo 的中和橫幅。
- **先比對、再登入**：`verify.sh --stage upgrade`（以及 Redeploy 後的那一次）要在任何人登入之前跑完。管理員一登入，
  Odoo 就把 `web.base.url` 改成登入用的網址——彩排站需要這個改動（之前產生的連結都指向來源站的正式網址），
  但之後再跑比對會看到 `ir_config_parameter` 的客戶資料列不同。
- **限制**：目標站仍有平台給的對外網址；升級帶進來的新排程會是啟用的（`verify.sh --stage upgrade` 的斷言會抓到，
  抓到就先把 web 停掉）；站別自己加的對外整合只能靠 `nscan` 與人工判斷。
- ⛔ 中和過的資料庫不得用於正式切換。正式切換重新走一次凍結匯出與不帶 `--neutralize` 的還原。

## 平台服務的停權與復權

平台沒有「還原中」狀態，也沒有給人按的停權按鈕（停權由訂閱生命週期觸發）；Redeploy 只開放給平台管理員
（`action_redeploy` 的 group gate），租戶頁面沒有。搬遷時由平台管理員在平台 Odoo pod 以 `odoo shell` 呼叫同一組方法，
包成 `scripts/platform/cloud-service.sh`（參數、前置 state、exit code 見檔頭）：

```bash
P=(--context woow-k3s --id <cloud_service id> --name <服務名稱> --release <svc-…>)
scripts/platform/cloud-service.sh status   "${P[@]}"   # 唯讀：state、訂閱狀態、conf、訂閱生命週期排程下次執行時間
scripts/platform/cloud-service.sh suspend  "${P[@]}"   # _do_suspend：web＋postgres 縮到 0、網址改指停權頁
scripts/platform/cloud-service.sh resume   "${P[@]}"   # _do_resume：全部開回 1、網址改回服務
scripts/platform/cloud-service.sh redeploy "${P[@]}"   # action_redeploy（非同步，用 status 等回到 running）
```

- 每次都先比對 id、名稱、release，有一個不符就不動手；動作只接受搬遷流程會遇到的 state（例如 resume 只接受
  suspended），欠費停權等其他情況要先查清楚。
- 走 `deploy/paas-odoo-platform-cron`，不寫 pod 名稱：平台升版後 pod 會換名（harumi 彩排第二輪撞過——寫死的 pod 名稱讓
  指令什麼都沒做，錯誤又被輸出過濾吃掉）。stg 平台加 `--platform-ns dev-sandbox-stg --deploy stg-odoo-dev-sandbox-odoo --db woow_stg`。
- 回 `ok: false` 或 `state: error`（operator 失敗）時 exit 1，看 JSON 的 `error`／`error_message`。prod 每一步人工放行。

⚠ 訂閱正常（含試用中）時，每天一次的訂閱生命週期排程會把停權中的服務 `_do_resume()` 開回來。停權前先跑 `status`
看 `lifecycle_nextcall_utc`，停權到復權之間不要跨過那次排程；還原途中若發現 web 被開回來，先停下來重新停權。

## 測試環境

`scripts/dev/fake-source.sh up|seed|down <slug> --context woow-k3s` 在 `dev-sandbox-<slug>` 起一個與 harumi-odoo
同形狀的假站（同一個 odoo-tenant chart 與 values、同一顆 odoo digest、pgvector:pg17、nginx sidecar、執行時 pip、
`/mnt/extra-addons` 與 data_dir 各一個客製 addon、vector 表）。它在白名單內，agent 可以直接對它跑步驟 1。
`up` 之後約 15 分鐘內要跑 `seed`：DB 初始化前 startupProbe 一直失敗，逾時會重啟容器。

`scripts/dev/fake-target.sh up|down <slug>` 在 `dev-sandbox-<slug>` 直接裝 woow-odoo chart 當目標（不經平台；ns 要先
帶 annotation `standalone-odoo-migrate/role=fake-target`）。兩個假站可以跑步驟 6～8：

- 負向（image 缺 addon ⇒ 升級閘 exit 4）：`fake-source.sh seed` 用預設的 fixture addon。
- 正向（升級閘跑 `-u all`）：`fake-source.sh seed --from-image <cloud-odoo ref> --extra-addons … [--data-dir-addons …] --pip "…"`
  讓來源改裝目標 image `/all-addons` 裡現成的 addon，`fake-target.sh up --addons` 勾同一批。不必另建 image。

平台層彩排（stg）：`/woow:deploy-stg --slug <新 slug>` 起一套新的 stg 平台 → `cloud_odoo_addon.sync_from_catalog()` →
以租戶身分照「購買目標服務」的 1～3 步建服務（`plan_id`、`odoo_addon_ids`、`values.conf.adminEmail` 都要帶）→ 目標就是
`paas-ws-stg-<hash>` 裡的 `svc-<id>`，步驟 6～8 照跑。2026-10-10 以 `fake-source.sh seed --from-image` 的來源跑通
（含停權、復權、Redeploy、重設管理員密碼）。

單元測試：`python3 -m unittest discover -s .claude/skills/standalone-odoo-migrate/tests`（`test_cron_hold_sql.py` 要一顆可丟棄的
PostgreSQL：設 `STANDALONE_MIGRATE_TEST_PSQL`＝psql 指令前綴，沒設就 skip）。

## 參考

- 偵察計畫（harumi 實例、Phase 0 指令區塊、風險表）：`docs/scout/2026-09-18-harumi-odoo-paas-migration-plan.md`
- 搬運 helper 形狀：`.claude/skills/woow-e2e-fixtures/scripts/seed-from-prod.sh`、
  `docs/guides/deployment/platform-odoo-cutover-runbook.md` 附錄 T
- standalone 站 chart 與 17 站 values：GitHub `WOOWTECH/Woow_k3s_odoo` 分支 `feat/tenant-chart`
