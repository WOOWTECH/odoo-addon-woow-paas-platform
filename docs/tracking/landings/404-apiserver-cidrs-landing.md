# project 404 apiserver cidrs landing

> 自 Claude memory 遷移（2026-08-05）。原 slug `project_404_apiserver_cidrs_landing`。盤點依據見 [memory-inventory-2026-08-05](../memory-inventory-2026-08-05.md)。
>
> 原 description："#404 apiServerCidrs injector 全案收官：PR #406（.75）+ stg/prod 參數落地 + stg E2E 零手動 patch PASS；prod 待 rollout 生效"

2026-07-22/23 #404 全案（woow-k3s post-DNAT NP 缺口令 open-design /console 開箱即壞）：

- **修法落地**：PR #406 merged（develop `15a5d5ff`、版號 18.0.1.0.75）。`cloud_service._inject_apiserver_cidrs` slug-scoped injector（unknown slug/空清單 no-op、寫入保持 list），值來自 `ir.config_parameter` `woow_paas_platform.apiserver_cidrs`（逗號分隔；code 預設 `10.43.0.1/32` cluster-agnostic）。cluster-specific node /32 不進 code/git seed，由 ops 設參數。
- **參數已設 stg + prod**（值 = `10.43.0.1/32,192.168.10.21/32,192.168.10.22/32,192.168.10.23/32`）：stg 在 `dev-sandbox-stg`（db `woow_stg`、conf `/etc/odoo/odoo.conf`）；prod 在 `paas-system`（db **`bitnami_odoo`**、conf `/opt/bitnami/odoo/conf/odoo.conf`）。雙信號驗證（fresh-process get_param + psql/raw-SQL 直查 row）。
- **stg E2E PASS**：stg 升 `paas-odoo-stg:20260722.1546`（digest 交叉驗非快取充數）+ `stg-upgrade-75` job → DB `.75 installed`；乾淨新建 open-design（ws2、`paas-ws-stg-ad023482`）**零手動 NP patch** 三信號全綠：① tenant NP egress 實含 4 CIDR ② console pod 內 connect.sh 同款 kubectl 查到 daemon pod（rc=0）③ 網頁終端經 CF tunnel 出 shell 可互動。測後清理：service delete + 手動刪殘留 ns（NS_GONE、PV_GONE）。
- **殘留 / 待辦**：prod 仍 `.73`——**下次 prod rollout 帶上 ≥.75 參數才生效**。既存 release（stg `my-od-1`）injector 不會補：injector 家族只在 `_execute_deploy()` install-time 跑，`action_redeploy()` reuse_values 不重跑——要把 `networkPolicy.apiServerCidrs` 顯式寫進 `svc.helm_values` 再 redeploy。
- **跨 app 稽核**（6 charts + live NP）：僅 open-design console 命中；agent-runtime/mcp-server/home-assistant 靠 `automountServiceAccountToken: false` 免疫；vibe-kanban 無 NP；paas-operator 目前 egress 未受限，日後給 `paas-system` 加 egress NP 須同時開 apiserver 洞（含 node IP 非只 ClusterIP）。
- 順帶實證：cloud service delete 已會連帶 cancel 訂閱（[[reference-stg-cloud-service-live-create-e2e]] 的 F-N8N-3 過時已更正）。

Link: [[project-opendesign-cs-onboarding-380]]、[[reference-stg-db-manual-upgrade-recipe]]
