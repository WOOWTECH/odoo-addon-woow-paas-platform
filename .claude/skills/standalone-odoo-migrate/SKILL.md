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
   - paas-odoo-ci PR（agent）：以步驟 3 的報告為準——`NEED_IMPORT` 的模組從 `<out-dir>/addons/` 複製進 `addons/`，
     `NEED_IMPORT`＋`NEED_LIST` 加進 `config/cloud-addons.list`；`scripts/check-addon-lists.sh` 要求 `addons/` 與
     `config/paas-addons.list` 完全相等，所以新目錄也要進那份清單（⇒ 會一起進平台自用的 paas-odoo image）。
     pip：`requirements-cloud.txt` 由 CI 從清單內模組 manifest 的 `external_dependencies` 產生，沒宣告的套件進不了
     image ⇒ 缺口補進用到它的模組 manifest（補了以後該模組重比時是 `DIVERGED`，PR 描述註明只差這一處）。
     `addons-export.json` 裡 `symlinks_outside` 非空的模組先處理連結。`DIVERGED` 與注意事項寫進 PR 描述給人裁決。
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
   完成條件：Job 印出 `RESTORE-DONE`，verify exit 0。
8. **升級驗證**（agent）：平台復權，等升級閘跑完、pod Ready → `scripts/target/verify.sh --stage upgrade --cleanup`。
   initContainer exit 4＝image 缺來源模組（DB 沒被動），回步驟 5；exit 78＝DB core 比 image 新，image 選錯。
   判準是客戶資料列（`scripts/fingerprint_business.sql` 檔頭）：還原當時就存在的客戶資料列必須原封不動；模組資料列
   被 `-u all` 重寫、升級後新增的列、registry 表的變動列為「預期內」。filestore：仍被附件引用的檔不能少、內容不能變；
   模組附件換了內容後被 Odoo 回收的舊檔列為「預期內」。
   完成條件：verify exit 0，「預期內的變動」逐條看過；`--cleanup` 才會刪掉含客戶資料的 incoming PVC。
   - 平台的 Redeploy 與改 addon 會觸發部署後初始化（operator `POST /init/odoo`：更新模組清單＋台灣在地化）。在地化會裝
     zh_TW、改管理員語言／時區與公司國家／幣別；restore 預寫的 `tenant_localized` 讓它整段跳過。要確認：第一次 verify 先不帶
     `--cleanup`，Redeploy 後再跑一次帶 `--cleanup` 的。
   - 匯入後管理員帳密是來源站的，平台存的那組不再適用；租戶可在服務頁按 Reset 重設（重設 `base.user_admin`、清掉 2FA）。
   ⚠ web 一開回來 cron 就開始跑（寄信、外部整合）。來源沒凍結的彩排（`export.sh --live`）會讓兩邊同時對外動作，
   彩排前先決定怎麼處理（#1604）。
9. **cutover**（hostname、外部整合改指、來源下線）：尚未實作，設計與進度在 #1604。

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
