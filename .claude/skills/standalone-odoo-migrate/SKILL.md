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
| 來源 ns（standalone 站，不在白名單） | woow-machines-operator（WMO） | `scripts/source/` ＋ `scripts/odoo_inspect.py`，連同指令交給 WMO；收回 JSON |
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
5. **addon 進 image、搬運、還原、cutover**：尚未實作，設計與進度在 #1604。

## 測試環境

`scripts/dev/fake-source.sh up|seed|down <slug> --context woow-k3s` 在 `dev-sandbox-<slug>` 起一個與 harumi-odoo
同形狀的假站（同一個 odoo-tenant chart 與 values、同一顆 odoo digest、pgvector:pg17、nginx sidecar、執行時 pip、
`/mnt/extra-addons` 與 data_dir 各一個客製 addon、vector 表）。它在白名單內，agent 可以直接對它跑步驟 1。
`up` 之後約 15 分鐘內要跑 `seed`：DB 初始化前 startupProbe 一直失敗，逾時會重啟容器。

單元測試：`python3 -m unittest discover -s .claude/skills/standalone-odoo-migrate/tests`。

## 參考

- 偵察計畫（harumi 實例、Phase 0 指令區塊、風險表）：`docs/scout/2026-09-18-harumi-odoo-paas-migration-plan.md`
- 搬運 helper 形狀：`.claude/skills/woow-e2e-fixtures/scripts/seed-from-prod.sh`、
  `docs/guides/deployment/platform-odoo-cutover-runbook.md` 附錄 T
- standalone 站 chart 與 17 站 values：GitHub `WOOWTECH/Woow_k3s_odoo` 分支 `feat/tenant-chart`
