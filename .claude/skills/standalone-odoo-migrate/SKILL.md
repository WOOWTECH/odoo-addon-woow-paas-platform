---
name: standalone-odoo-migrate
description: "把 standalone Odoo 站（odoo-tenant chart 收編的 *-odoo 站，例 harumi-odoo）搬進 PaaS 指定租戶的 Odoo 服務：盤點、相容性比對、addon 進 cloud-odoo image、還原、資料一致驗證、切換。"
when_to_use: 使用者要把 standalone／odoo-tenant 站搬進 PaaS、盤點某個 *-odoo 站能不能搬、比對站點 addon 與 cloud-odoo image，或要在沙盒起假 standalone 站測搬遷流程。
---

# standalone Odoo → PaaS 租戶搬遷

兩條硬需求（#1604）：**資料與來源逐位元一致**；**addon 由 paas-odoo-ci 烘進 cloud-odoo image**（不放 PVC）。
租戶帳號、workspace、購買與擁有權不在本 skill：由人照正常購買流程先買好目標服務，本 skill 只接收它的 `cloud_service` id。

## 分工

kubectl 白名單（CLAUDE.md 鐵律）決定誰跑哪一段：

| 段 | 執行者 | 交付物 |
|---|---|---|
| 來源 ns（standalone 站，不在白名單） | woow-machines-operator（WMO） | `scripts/source/` ＋ `scripts/odoo_inspect.py`、`scripts/addons_unpack.py`，連同指令交給 WMO；收回 JSON 與導出的 addon 目錄 |
| 目標（`dev-sandbox-*`、`paas-ws-stg-*`、`paas-ws-*`） | 本 repo agent | prod 寫入每一步都等人工放行 |
| paas-odoo-ci PR merge、prod image `/approve` | 人 | agent 只開 PR |

## 步驟

每支腳本的參數與 exit code 寫在檔頭（`sed -n '2,20p' <script>`），這裡不重複。

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
     - pip：`requirements-cloud.txt` 由 CI 從清單內模組 manifest 的 `external_dependencies` 產生。manifest 寫的是
       import 名而 pip 裝不了時（例 `PIL`），在 paas-odoo-ci 的 `generate-requirements.sh` 對照表補一筆，不改 manifest。
     - 匯入前掃一次寫死的密碼／金鑰；`addons-export.json` 裡 `symlinks_outside` 非空的模組先處理連結。
     - `cloud-addons.list` 一動，平台 image 也要重 build（catalog 在平台 image 裡，平台才看得到新 addon）。
   - build 出新 cloud-odoo 後，目標服務改用這顆 image，回到步驟 2 重比到 `READY`。data_dir 的 addon 也要進 image：
     升級閘只讀 image 的 `addons_path`，不讀 data_dir。完成條件：目標 image 的盤點涵蓋來源所有已安裝模組。
6. **搬運**：目標端 `scripts/target/receive.sh open`（agent）印出 `--url` 並寫出 token 檔 → 兩者交給 WMO 跑
   `scripts/source/export.sh`（預設凍結：來源 odoo 先 scale 0；`--live` 只給彩排）→ `receive.sh close`。
   完成條件：`export-<ns>-<UTC>.json` 每個檔的 sha256 都跟 receiver 回報一致（export.sh 失敗即 exit 3）。
7. **還原**（agent）：平台停權 → postgres 開回 1 → `scripts/target/restore.sh --replace-db` → `scripts/target/verify.sh --stage restore`。
   - 停權走平台（見下方「平台服務的停權與復權」）：web 與 `<release>-postgresql` 都會縮到 0、對外網址改指停權說明頁。
     restore Job 要連 PG ⇒ 停權後 `kubectl scale sts/<release>-postgresql --replicas=1` 並等它 Ready。
   - restore Job 內已逐位元比對，一致才留下客戶資料列基準（`baseline.bfp`）並預寫兩個標記：`upgrade_done`（來源 image
     digest＋core_date，讓 web 開機時升級閘自動跑 `-u all`）與 `tenant_localized`（擋掉平台的台灣在地化，見步驟 8）。
   - **彩排**（來源還在營運）一律加 `--neutralize`，見下方「彩排與中和」。正式切換不加。
   完成條件：Job 印出 `RESTORE-DONE`，verify exit 0。
8. **升級驗證**（agent）：平台復權，等升級閘跑完、pod Ready → `scripts/target/verify.sh --stage upgrade --cleanup`。
   initContainer exit 4＝image 缺來源模組（DB 沒被動），回步驟 5；exit 78＝DB core 比 image 新，image 選錯。
   判準是客戶資料列（`scripts/fingerprint_business.sql` 檔頭）：還原當時就存在的客戶資料列必須原封不動；模組資料列
   被 `-u all` 重寫、升級後新增的列、registry 表的變動列為「預期內」。模組擁有的列（含 noupdate）的可翻譯欄位只比
   en_US——升級會替它們補上新翻譯。filestore：仍被附件引用的檔不能少、內容不能變；
   模組附件換了內容後被 Odoo 回收的舊檔列為「預期內」。
   完成條件：verify exit 0，「預期內的變動」逐條看過；`--cleanup` 才會刪掉含客戶資料的 incoming PVC。
   比對要在任何人登入目標站之前跑：管理員登入會改寫 `web.base.url`，之後的比對會多出 `ir_config_parameter` 不一致。
   session 檔不在搬遷範圍：搬過去之後所有人都要重新登入（Odoo 的 autovacuum 會把舊的裝置紀錄標成已撤銷）。
   - 平台的 Redeploy 與改 addon 會觸發部署後初始化（operator `POST /init/odoo`：更新模組清單＋台灣在地化）。在地化會裝
     zh_TW、改管理員語言／時區與公司國家／幣別；restore 預寫的 `tenant_localized` 讓它整段跳過。要確認：第一次 verify 先不帶
     `--cleanup`，Redeploy 後再跑一次帶 `--cleanup` 的。
   - 匯入後管理員帳密是來源站的，平台存的那組不再適用；租戶可在服務頁按 Reset 重設（重設 `base.user_admin`、清掉 2FA）。
   ⚠ web 一開回來 cron 就開始跑（寄信、收信、金流、外部整合）。來源沒凍結的彩排（`export.sh --live`）不中和的話，
   兩邊會同時對外動作 ⇒ 彩排的還原一定要 `--neutralize`。
9. **cutover**（正式切換；prod 每一步人工放行）。前置：彩排通過、目標服務已由人在 prod 租戶買好、下方「切換前檢查」
   逐項有結論。停機時間＝凍結匯出＋還原＋開機升級＋比對（harumi 的量：約 15 秒＋45 秒＋3 分鐘，另加人工步驟）。
   1. 目標服務先調好設定（「切換前檢查」的 conf 覆寫、需要時 `shared-mail.sh open`），Redeploy 完成、服務 running。
   2. 凍結來源（WMO）：公告停機 → POS session 全部關帳、沒有 draft 訂單 → 來源 odoo scale 0（postgres 留著）。
   3. 走步驟 6～8：`export.sh` **不帶** `--live`、`restore.sh` **不帶** `--neutralize`。兩階段 verify 都 exit 0 才往下；
      不通過就回切（第 6 點），不要帶著差異上線。比對在任何人登入之前跑完。
   4. 對外網址改指（下方「對外網址」）、外部整合改指（MCP、webhook 來源端的設定）。
   5. 驗收（人）：登入、開一個 POS session、收發各一封信、金流測一筆。
   6. 回切：新站產生第一筆真實交易之前，把來源 odoo scale 回 1、網址指回來源即可——匯出全程唯讀，來源資料沒被動過。
      之後就不能回切（兩邊資料分岔）。
   7. 收尾：`verify.sh --stage upgrade --cleanup` 刪掉含客戶資料的 incoming PVC；來源保留到約定的觀察期滿再下線（WMO）。

## 切換前檢查

每一站在彩排後、排切換日之前逐項確認；`compat_check.py` 的「注意事項」會列出其中幾項。

- **方案與資源**：目標的 `workers` 與記憶體上限由購買的方案決定（`cs_odoo_workers`／`cs_ram_gb`），不能逐台覆寫。
  來源的 `workers`、`limit_memory_*` 用來選方案；選小了，切過去會變慢或 worker 被記憶體上限中止。
- **conf 覆寫**：`limit_time_cpu`／`limit_time_real`／`server_wide_modules` 不在方案內，chart 預設是 60／120／`base,web`。
  平台管理員把 `conf.limitTimeCpu`、`conf.limitTimeReal`、`conf.serverWideModules` 寫進服務的 `helm_values` 後 Redeploy
  （stg 實測：odoo.conf 生效、`server_wide_modules` 帶 `/all-addons` 的模組可以開機）。
- **寄收信主機**：盤點的 `db.mail_hosts`。conf 層的 SMTP 不會帶過去（改用 DB 的 `ir.mail_server`）。叢集內位址
  （`*.svc.cluster.local`）從租戶 ns **連不到**——baseline egress 只放行同 ns、DNS 與對外網路。
  - 指向 **mailcow**（woow-k3s 的共用 mail 服務，不在 PaaS 內，歸 woow-machines-operator 管；只有叢集內位址）：
    目標服務買好、running 之後跑 `scripts/target/shared-mail.sh open`——目標 ns 加准入 label、加一條只開給這台 Odoo
    的 egress 規則，並實測連線。每一站接上之前先取得使用者同意（label 是整個 ns 的，同工作區的其他服務在 mailcow
    那一側也會被放行）。stg 實測：接上後 587／993 連得到、同 ns 其他 pod 仍被擋、Redeploy 後仍在、`close` 可收回。
  - mailcow 用私有 CA：收信靠 `mail_fetchmail_durable` 內附的 `mail-ca.crt` 驗憑證（stg 實測可驗）⇒ 該模組要在
    `conf.serverWideModules` 裡。
  - 其他叢集內主機沒有現成作法，要先與該服務的負責人談。
- **對外網址**：要不要沿用來源的 hostname（印在桌上的 QR code、金流回呼、外部系統都指向它）。見下方。
- **外部整合**：連到來源內部位址的東西（MCP server、其他服務的 webhook）切換後要改指新服務。
- **登入**：session 不搬，所有人要重新登入；平台開通時給的管理員帳密不適用，帳密是來源站的。
- **金流與發票**：正式切換不中和，來源的金流供應商、發票憑證原樣帶過去，回呼網址跟著對外網址走。

## 對外網址

平台服務自帶一個 `<subdomain>.<平台網域>` 網址。要讓來源的 hostname 繼續指到新服務，用 operator 的 route API 另加一條：

- `POST /api/routes`，body `{"subdomain": "<來源 hostname 的第一段>", "service_url": "http://<release>.<ns>.svc.cluster.local:80"}`
  ——在平台 tunnel 加 ingress rule 並建 DNS CNAME。只適用於與平台同一個網域的 hostname。
- ⚠ 那筆 DNS 記錄還指向一條**活著的** tunnel 時 operator 會拒絕（`_assert_dns_repoint_allowed`）：切換時先由來源 tunnel 的
  管理者刪掉舊的 DNS 記錄（或整條來源 tunnel），再呼叫。順序＝來源下線 → 刪舊記錄 → 加新 route。
- stg 實測（額外 hostname 指到彩排站）：加上後可用；Redeploy 後仍在；**停權時它回 502**，不會像服務自己的網址改指
  停權說明頁；`DELETE /api/routes/<subdomain>` 可移除。
- 平台不認得這條 route：刪服務時不會一起刪，要手動移除。Odoo 端的 `web.base.url` 是來源帶過來的原值，不必改。
- prod 的 route 是 prod 寫入，由人放行後執行；回切＝刪掉這條 route、把 DNS 指回來源 tunnel。

## 彩排與中和

彩排＝來源站照常營運，用 `export.sh --live` 取一份資料到目標站走完步驟 6～8。目標站拿到的是真實資料與真實的對外設定，
開機前必須中和：`restore.sh --replace-db --neutralize`。

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

平台沒有「還原中」狀態，也沒有給人按的停權按鈕（停權由訂閱生命週期觸發）。搬遷時在平台 Odoo pod 以 `odoo shell` 直接呼叫
同一組方法，交易要自己 commit：

```python
s = env['woow_paas_platform.cloud_service'].browse(<cloud_service id>)
s._do_suspend(); env.cr.commit()   # web＋postgres 縮到 0、網址改指停權頁、state=suspended
s._do_resume();  env.cr.commit()   # 全部開回 1、網址改回服務、state 回到停權前的值
```

呼叫後看 `s.state` 與 `s.error_message`：operator 失敗時 state 會是 `error`。prod 的平台 pod 屬 prod 寫入，每一步人工放行。

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
以租戶身分打 `/api/workspaces`、`/api/workspaces/<id>/services`（`odoo_addon_ids` 勾來源用到的 addon）建服務 → 目標就是
`paas-ws-stg-<hash>` 裡的 `svc-<id>`，步驟 6～8 照跑。2026-10-10 以 `fake-source.sh seed --from-image` 的來源跑通
（含停權、復權、Redeploy、重設管理員密碼）。

單元測試：`python3 -m unittest discover -s .claude/skills/standalone-odoo-migrate/tests`。

## 參考

- 偵察計畫（harumi 實例、Phase 0 指令區塊、風險表）：`docs/scout/2026-09-18-harumi-odoo-paas-migration-plan.md`
- 搬運 helper 形狀：`.claude/skills/woow-e2e-fixtures/scripts/seed-from-prod.sh`、
  `docs/guides/deployment/platform-odoo-cutover-runbook.md` 附錄 T
- standalone 站 chart 與 17 站 values：GitHub `WOOWTECH/Woow_k3s_odoo` 分支 `feat/tenant-chart`
