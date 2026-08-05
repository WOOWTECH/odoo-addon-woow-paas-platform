# project 425 pg netpol hotfix

> 自 Claude memory 遷移（2026-08-05）。原 slug `project_425_pg_netpol_hotfix`。盤點依據見 [memory-inventory-2026-08-05](../memory-inventory-2026-08-05.md)。
>
> 原 description："#425 prod 主 DB NetworkPolicy from-less S1 全案閉環：兩步 commit 皆生效、prod 收斂完成零 downtime、#425 closed"

2026-07-25。prod `paas-system/paas-odoo-postgresql` NetworkPolicy from-less（bitnami chart `allowExternal=true` 預設）＝叢集內任意 pod 可達 5432。載體是 **paas-app-bundle-cd**（Fleet bundle `paas-app-bundle-paas-odoo`），非 charts repo、非 addon repo。

**狀態**：
- PR #59（step 1：`postgresql.primary.networkPolicy.extraIngress` odoo 白名單，`allowExternal` 維持 true）✅ merged，main=`d7de470a`，Fleet 20 秒 sync，pg NP 出現第 2 條 rule，odoo/pg pod RESTARTS=0、AGE 未歸零，health/login 200、`PG_REACHABLE`。
- PR #60（step 2：`allowExternal: false`，+1 行）✅ Eugene 親按 merged，main=`056f8106`，Fleet sync，leader 獨立驗收：**pg NP from-less rule 歸零**（rule[0]=pg自己+client-label、rule[1]=odoo podSelector）、odoo pod python socket 連 pg OK、health/login 200、RESTARTS=0。**#425 closed（S1 核心解、零 downtime）**。
- ⚠ 驗收踩雷：`sh -c '… /dev/tcp …'` 在 odoo pod（/bin/sh→dash）假陰性報 PG_UNREACHABLE，差點誤判斷線；python socket 複驗擋下。詳見 [[reference-dash-devtcp-false-negative]]。
- 衍生 #427（paas-system default-deny baseline）、#428（odoo `containerPorts.https` 未定義→pod 全 TCP port 開放；**不可與熱修併改**，會觸發 Recreate + startupProbe 180s ＝ prod 全站 4–6 分鐘 downtime）。

**兩個假綠陷阱（bitnami postgresql subchart，下次改 NP 必查）**：
1. key 路徑是 `postgresql.primary.networkPolicy`（模板讀 `.Values.primary.networkPolicy.*`）；寫成 `postgresql.networkPolicy` 會**helm 不報錯、Fleet 顯示 sync 成功、live NP 完全沒變**。驗法 `yq '.postgresql.networkPolicy'` 必須 null。
2. 必須併進既有 `postgresql:` 區塊；另起第二個同名頂層 key 會整段覆蓋前者，炸掉 image pin 與 auth。驗法 `grep -c '^postgresql:'` = 1。

**收斂後運維影響（會誤判成「DB 掛了」）**：只有帶 `app.kubernetes.io/name=odoo` + `instance=paas-odoo` 的 pod 能連 5432 —— prod 過去做首裝／module update 的 one-off Job 若不帶 label 會連不上，須補 `paas-odoo-postgresql-client: "true"`。port-forward 到 pg 是否受影響未實測。

**有效工作法**：改 values 前先用真實 values 跑本地 `helm template` 對 base 做全 manifest diff，逐 kind 比對 Deployment/StatefulSet/Service/Ingress ＝ `IDENTICAL`，就能在動 prod 前證明「零 pod 重啟」；密碼類隨機欄位要 `--set` 固定否則 diff 有噪音。相關 [[feedback_never_fabricate_tool_output]]、[[feedback_shared_cluster_paas_scope_guard]]。
