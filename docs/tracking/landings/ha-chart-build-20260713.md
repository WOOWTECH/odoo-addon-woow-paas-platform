# project ha chart build 20260713

> 自 Claude memory 遷移（2026-08-05）。原 slug `project_ha_chart_build_20260713`。盤點依據見 [memory-inventory-2026-08-05](../memory-inventory-2026-08-05.md)。
>
> 原 description：woow-paas-charts 新增 home-assistant chart（供 paas-platform

2026-07-13 在 woow-paas-charts repo 建 `charts/home-assistant` chart，供 paas-platform CloudAppTemplate pin 跑 HA 官方 image（Stopgap，[[project_ha_paas_provision_audit_20260713]] #336 工作項5 需要的輕量 HA 載體）。

**交付**：PR **#40**（`feat/ha-chart`→main，git-prod woow-paas-charts）；commit 5963ba9；`Refs #336`。

- **OCI 座標（供 helm_chart_name）**：`oci://jcr-prod.woowtech.io/woow-paas-docker-local/home-assistant`，version `0.1.0`。**merge main 後**才由 CI（`.gitea/workflows/publish-charts.yaml`，push main + charts/** 變更觸發）push OCI；PR 本身只跑 `test-charts.yaml`（render 斷言）。OCI 憑證已配（mcp-server 已 OCI-pin 佐證）。
- **模板來源**：mcp-server（per-instance 單容器 + RWO PVC + Recreate + seed initContainer 慣例）＋ vibe-kanban 的通用 Ingress。
- **可覆蓋 values**：`image.repository/tag`（default `ghcr.io/home-assistant/home-assistant:2026.7.2`）、`persistence.mountPath=/config`+`size=5Gi`、`service.port=8123`(ClusterIP)、`ingress.enabled`(default off，host 平台帶)、`haProxy.trustedProxies`(default `10.42.0.0/16`)、`resources`(1vCPU/1GB)。
- **關鍵解 HA 反代 400**：ConfigMap 渲染整份 configuration.yaml（`default_config:` + `http.use_x_forwarded_for` + `trusted_proxies`）→ **seed-if-absent** initContainer cp 到 `/config/configuration.yaml`。
- **⚠ seed-if-absent = 首裝限定**：檔存在後 `helm upgrade` 改 trustedProxies **不 re-seed**；平台務必首次 provision 就設實際 Pod CIDR，否則 wrong CIDR 靜默續 400。

**設計要點（advisor 審過）**：
- HA 官方 image 走 root + s6-overlay → `securityContext` 預設放寬（強制 non-root/drop-ALL 會炸 init，實測 log 見 s6-rc 啟動）。
- HA 冷啟慢 → `startupProbe`(tcpSocket 8123, failureThreshold 40) 護 liveness（實測 startupProbe「connection refused」正常吸收冷啟）。
- proxy test **重新 parse 內嵌 configuration.yaml YAML tree**（雙 yq）而非 grep——內嵌是字串 literal，縮排壞掉會靜默過 grep 但 HA 忽略 http 區塊續 400。

**實測（docker-desktop，全綠）**：HA image 首拉 608MB 約 6m28s；pod 1/1 Ready；`/config/configuration.yaml` 正確 seed；`home-assistant.log` 0 ERROR/CRITICAL；`http://127.0.0.1:8123/` 回 HTTP 200 text/html（http config 被接受）；HA 產物（.HA_VERSION/.storage/db/blueprints/tts）齊全。

**OCI 落地**：PR#40 merged → publish CI run 902 **首跑紅**（步驟(1) Gitea Helm-repo push `POST .../helm/api/charts` 回 HTTP 500，因 workflow `set -e`+`curl -f` abort，卡在 OCI push(2) 之前；OCI 未發）→ leader **re-run 902 綠**（500 是 transient；package 半建成→re-run GET=200 跳過 Gitea push→直接 OCI）→ log `OCI: published oci://.../home-assistant:0.1.0`＋`Job succeeded`；**helm pull 實證**：`Pulled ... home-assistant:0.1.0`，digest sha256:73b50816…，metadata name=home-assistant/version=0.1.0/appVersion=2026.7.2。**OCI 就緒可 pin。**

**publish-charts.yaml 脆弱性（follow-up）**：Gitea-repo push 與 OCI push 用 `set -e` 綁死——Gitea-repo transient 500 會誤擋不相干的 OCI push。平台只要 OCI，卻被 Gitea-repo 步驟卡。建議解耦（Gitea-repo push 失敗改 warn 不 abort）。leader 選「先 re-run 測 transient」已解本次；硬化 PR 未開。

**球在**：paas-platform 填 helm_chart_name=`oci://jcr-prod.woowtech.io/woow-paas-docker-local/home-assistant` + version 0.1.0 + helm_default_values 建 CloudAppTemplate（trusted_proxies 待拍板 ingress/tunnel 後給實際 Pod CIDR）。
