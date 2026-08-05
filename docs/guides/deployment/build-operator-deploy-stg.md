# reference build operator deploy stg

> 自 Claude memory 遷移（2026-08-05）。原 slug `reference_build_operator_deploy_stg`。盤點依據見 [memory-inventory-2026-08-05](../../tracking/memory-inventory-2026-08-05.md)。
>
> 原 description：paas-operator 建置+部署 stg 全流程 gotcha — operator-v* tag 不觸發 CI（改 workflow_dispatch）、Fleet 落點、驗證法

**paas-operator 建置 → 部署 stg 全流程**（2026-07-05 #279/#280 部署實證）：

**① build-operator skill 的 `operator-v*` tag 機制在此 Gitea 上壞掉（must-know）**：`.gitea/workflows/build-paas-operator.yml` 的 `push` trigger 同時有 `paths: extra/paas-operator/**` 與 `tags: operator-v*`——Gitea Actions 對 tag push 套 `paths` filter 把它**吃掉、不產生 run**（push tag 成功但 CI 靜默不觸發）。改用：`tea actions workflows dispatch build-paas-operator.yml --repo odoo-addons/woow_paas_platform --login eugene --ref develop --input tag=<YYYYMMDD.HHMM>`（workflow 有 `workflow_dispatch.inputs.tag`，冪等）。**2026-08-01 operator-v20260801.1305 再次實證**：tag push 依然靜默不觸發；且 dispatch 想釘在 tag 上時 `--ref` 用 tag 名會被拒（ref doesn't exist），必須寫全名 **`--ref refs/tags/operator-v<版>`**。另：operator 的 values-prod.yaml image 段**無 digest pin 欄位**（只有 repository/tag/pullPolicy，digest 僅記註解）——「digest pin 優先於 tag、只改 tag 是 no-op」的教訓屬 paas-odoo values，operator 檔改 tag 即生效，勿捏造 digest 欄位。

**② operator docker build ~10-18s 是正常的**（act-runner-runner-0 layer cache 熱）；`#N CACHED` 全 CACHED＝「同一 develop HEAD 之前建過」非「舊碼」。驗證真的建對：`tea actions runs view <id>`（Conclusion: success）+ `tea actions runs logs <id> | grep 'digest:'`（docker push 成功印 `<tag>: digest: sha256:… size:`）+ 比對 registry digest ≠ 現況 pod imageID digest（排除 cache 供舊碼）。

**③ Fleet 落點**：單一 GitRepo `paas-app-bundle`（ns `fleet-local`）watch `woow-paas/paas-app-bundle-cd` 的 **`main` branch**（repo 預設分支 `master` 是空的、別被騙）。operator image pin：
- stg → `paas-operator-stg/values-stg.yaml`（`image.tag`）
- prod → `paas-operator/values-prod.yaml`
改 stg 只動 stg 檔、commit+push `main` → Fleet ~1-2 分鐘 reconcile → deployment rolling。**`git push` 到此 CD repo 會撞 auto-mode 權限**（需使用者授權 / `Bash(git push:*)` 規則 / 使用者 `!` 前綴自跑）。

**④ 驗證 rollout**：`kubectl -n paas-stg-system get deploy paas-operator-stg -o jsonpath image` == 新 tag + `readyReplicas=1` + 新 pod 0 restart；F6 guard 過看 pod log `F6 env guard OK — PAAS_ENV=stg, namespace_prefix=paas-ws-stg-`（PAAS_ENV≠prefix env 會 lifespan fail-fast CrashLoop）；新 endpoints 存在 `kubectl exec <pod> -- python3 -c '…openapi.json…'` 撈 paths。stg operator env（PAAS_ENV=stg / NAMESPACE_PREFIX=paas-ws-stg-）在 values-stg.yaml `config.paasEnv`+`config.namespacePrefix`。

相關：[[deploy-stg-gotchas]]（odoo image 走 tea tag，與 operator 不同路）；[[agent-runtime-respawn-mechanism]]（release chain）；[[netpol-279-280-landing]]。
