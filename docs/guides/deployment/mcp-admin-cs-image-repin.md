# reference mcp admin cs image repin

> 自 Claude memory 遷移（2026-08-05）。原 slug `reference_mcp_admin_cs_image_repin`。盤點依據見 [memory-inventory-2026-08-05](../../tracking/memory-inventory-2026-08-05.md)。
>
> 原 description：standalone MCP admin cloud service（odoo/n8n/hermes）映像重釘＋rollout recipe，含 woow-mcp-server CI paths-filter 陷阱（各 app 只有其變更的 push 才 build）

3 個 standalone MCP admin cloud service（**odoo/n8n/hermes**，全在 stg ns `paas-ws-stg-86f34bd4`）= Helm release `svc-83fa4bf9`(odoo) / `svc-8e4f5042`(n8n) / `svc-0246586d`(hermes)，經 mcp-server OCI chart `oci://jcr-prod.woowtech.io/woow-paas-docker-local/mcp-server` **0.1.5** 部署。app 映像 = `jcr-prod.woowtech.io/woow-paas-docker-local/<app>-mcp-admin`，source repo = **woow-mcp-server**（本地 `~/Documents/woow/woow-paas-repo/WOOWTECH/woow-mcp-server`）。

**pin 落點（兩軌，同 [[project_netpol_279_280_landing]] 的 #280 seed-bump+helm-upgrade 型）**：
- **git（durability）**：本 repo `src/data/cloud_app_templates.xml` 三筆 template（`cloud_app_mcp_hermes/odoo/n8n`）的 `helm_default_values` JSON `image.tag`（per-app）。noupdate=0 → 升級收斂；配 manifest patch bump（migrations 非每版必要，純值改動無需 healer）。live release `helm get values` 與 seed 字串**完全一致**（seed 是權威）。
- **live（rollout）**：既存 pod **不會**因 seed 改動自動 roll（見 [[reference_agent_runtime_respawn_mechanism]] 同家族）→ 必須 `helm upgrade` 既存 release 才換映像。

**image.tag 格式 = `<40char-git-sha>@sha256:<digest>`（雙釘）**。containerd 用 digest（digest 勝），故 digest 必須對；填錯 digest＝ImagePullBackOff 或拉到舊映像。

**解 digest（憑證勿落 transcript，classifier 會擋 base64-decode+echo）**：從 `paas-registry` pull secret 取 jcr 帳密寫進暫存 curl config（只寫檔不 echo），查 registry v2：`GET https://jcr-prod.woowtech.io/v2/woow-paas-docker-local/<app>-mcp-admin/manifests/<tag>`（`Accept: .../manifest.v2+json` 等）→ header `Docker-Content-Digest`。順帶當存在性檢查（200 vs 404）與 `.../tags/list` 列 tag。

**rollout recipe（本機 helm，2026-07-07 實跑 PASS）**：
1. `helm registry login jcr-prod.woowtech.io -u <u> --password-stdin`（帳密同 paas-registry；密碼走 stdin、user 走 `$()` arg，皆不落 transcript）。
2. 逐 release：`helm --kube-context woow-k3s -n paas-ws-stg-86f34bd4 upgrade <release> oci://jcr-prod.woowtech.io/woow-paas-docker-local/mcp-server --version 0.1.5 --reuse-values --set-string image.tag='<sha>@sha256:<digest>' --wait --timeout 300s`。`--reuse-values` 只換 tag、其餘（app/envPrefix/config.nonSensitive/n8n self-gen authToken via chart lookup）原封。`--set-string` 避免 `@`/`:` 被型別化（值無逗號故安全）。
3. 驗證：pod `containerStatuses[0].imageID` 的 digest == 目標 + `exec python -c urlopen localhost:8080/healthz` 回 200。**updateStrategy=Recreate → 每 CS 短暫 downtime**（預期）。helm `APP VERSION` 欄不變（= chart appVersion metadata，非部署映像，別誤判沒換）。

**⚠️ woow-mcp-server CI paths-filter 陷阱（`.gitea/workflows/ci.yml`）**：build matrix `[hermes,n8n,odoo]`，每 app step `if: needs.changed.outputs[matrix.app]=='true'`（`dorny/paths-filter`：`apps/<app>/**` + 共用 `packages/mcp-admin-{core,ui}/**` + `.gitea/workflows/**`）。**push 事件比對「前一個 push」**。→ 一個只改 `apps/n8n`+`apps/hermes` 的 merge（例 `ed098b6`=PR#10 n8n/hermes strip）**不會 build odoo** → registry **沒有** `odoo-mcp-admin:ed098b6`（**正確行為、非漏建**）。**每個 app 要釘到「真正 build 出它的 commit」**：2026-07 這次 odoo→`a447a80`(PR#9，含 odoo 四修正)、n8n/hermes→`ed098b6`(PR#10)。映像**恆從完整 HEAD tree build** → n8n/hermes@ed098b6 仍含 PR#9 共用 mcp-admin-ui 的 tokens 頁變更。**別無腦三個都釘同一 HEAD sha**（會 404）。push 走叢集內網 Artifactory nginx（繞 CF 上傳上限）。

**2026-07-07 執行**：PR **#309**（seed bump `18.0.1.0.43→44`，base develop，未 merge）+ 三 release 本機 helm upgrade → rev 2→3、pod fresh Running 0-restart、healthz 200、imageID digest 三者相符。worktree `.claude/worktrees/fix+mcp-admin-connection-rollout`（分支已推）。
