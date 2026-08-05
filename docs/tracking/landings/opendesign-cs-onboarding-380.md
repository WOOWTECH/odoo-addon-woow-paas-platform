# project opendesign cs onboarding 380

> 自 Claude memory 遷移（2026-08-05）。原 slug `project_opendesign_cs_onboarding_380`。盤點依據見 [memory-inventory-2026-08-05](../memory-inventory-2026-08-05.md)。
>
> 原 description："OpenDesign 完整應用上架 #380 全案收官（closed）：seed 已上 develop .74，但端到端部署撞 3 chart runtime 缺陷 + 1 quota 建模缺口 → 4-PR 修復鏈全 merged；含兩個可複用教訓"

**✅ 2026-07-21 全案收官（#380 closed）**。三線（A chart PR#41／B image PR#15／C seed）皆 merge、seed 上 develop（.74）。但**端到端實部署一個 open-design 服務時對外回 CF 502**——seed 上架 ≠ 服務可用。逐層剝出 **3 個疊加 chart runtime 缺陷 + 1 個平台 quota 建模缺口**，4-PR 修復鏈全 merged：

| PR | 修 | commit |
|---|---|---|
| #398 | seed `min_ram_gb` 2→3（quota 蓋得住 daemon）| 3e7dc3d2 |
| #399 | 平台部署注入 `config.allowedOrigins`（公開 Origin 白名單）| 4d38cee9 |
| woow-paas-charts#48 | chart 0.1.1：daemon origin 崩 + authproxy OOM | 76170001 + OCI publish |
| #400 | seed 釘 chart 0.1.1（讓上述生效）| c82b5a58 |

**責任鏈**：CF 502 ← svc endpoints 空 ← daemon 0 可用 ← ①quota 擋排程 ②daemon `OD_ALLOWED_ORIGINS=*`→`new URL('*')` 崩 ③authproxy 256 worker OOM ④公開 Origin 未白名單 403 ← seed pin 0.1.0 抓不到修好 chart。

## 可複用教訓（跨 app 會重現）

1. **quota 建模缺口**（`cloud_service._compute_ns_quota`）：workspace ns quota mem = `max(sum(存活 service allocated_ram_gb)×3, 8)`Gi；`allocated_ram_gb = plan.cs_ram_gb or template.min_ram_gb`（OpenDesign plan cs_ram_gb=0 → 走 template.min_ram_gb fallback）。兩個盲點：(a) **多容器服務的 sidecar 記憶體不入帳**（authproxy 128Mi + console 512Mi 沒算）；(b) 公式假設 chart `limits ≤ 3× 宣告 min`，但 chart daemon `limits.memory=8Gi` = 4× requests(2Gi)。OpenDesign 全容器 pod limits = daemon 8Gi + authproxy 128Mi + console 512Mi = **8832Mi**，需 `min_ram_gb ≥ ceil(8.63/3)=3`。守線 test = `test_min_ram_covers_daemon_quota`（min_ram_gb×3 ≥ daemon limits.memory）。**新上架多容器/高記憶體 app 必查此帳**。

2. **瀏覽器 app 的 Origin 白名單要平台注入**：daemon 若對**公開**租戶 Origin 只認 verbatim 白名單（同源 fallback 只信 loopback/private-LAN host，見 open-design `isAllowedBrowserOrigin`），**chart 拿不到租戶 host**（平台走 Cloudflare tunnel、沒傳 `ingress.host` 給 chart，`helm get values` 實查無 ingress）。故**平台必須部署時算 `https://{subdomain}.{paas_domain}` 注入**（`cloud_service._inject_allowed_origins`，slug-scoped path_map、比照 `_inject_storage_class` static injector 模式，unknown slug no-op）。chart 只能防崩（`*`/空→daemon 不崩）。

3. **chart 兩缺陷 helm-template/lint tests 抓不到、需真部署 smoke**：`OD_ALLOWED_ORIGINS=*`→daemon `new URL('*')` ERR_INVALID_URL 啟動崩；nginx `worker_processes auto`（非 cgroup-aware）於 256 核 node fork 256 worker → 破 authproxy 128Mi limit → OOMKilled(137)。修法：configmap 把 `*`/空視為 unset-omit；auth-proxy-configmap 加主 nginx.conf 釘 `worker_processes`（鏡射 nginx-unprivileged 預設、pid/temp 留 /tmp）+ deployment 掛載改 subPath。**驗證務必真部署**（`helm upgrade` 既有 stg 服務 + `--set` origin → daemon 2/2 + 對外 401/200，先前全 502）。

## 原始拍板記錄（仍有效）
單級距 4,500/月、36,000/年（×8）＋+1access 5,100/40,800；auth **C 案**＝nginx sidecar basic-auth 閘（#357 代生成 admin_password＝租戶密碼、OD_API_TOKEN chart 自產內部化、daemon 綁 loopback、Service :7457→proxy 8080、has_admin_gui=True）；上游 v0.15.1（bearer 啟用會讓自家 UI 死→用 basic-auth 閘不開 daemon token）。決策留言 #380 2026-07-20 00:25。

**✅ deploy-time 全自動 E2E 已跑並 PASS**（2026-07-21，stg image 20260721.1423=develop 含 4 PR）：乾淨 ws2 前台 API 新建 open-design 服務（svc46）**零手動**→ namespace 自動 quota 9Gi、helm values 自動注入 config.allowedOrigins=真租戶 URL、chart 0.1.1、daemon 2/2、endpoints 非空、外部 URL 401(閘)/api/health 200/帶對密碼進 UI 200。測試資源已全清（svc/訂閱/ns/quota 復原、n8n 保留）。#380 真正端到端收官。E2E recipe＝[[reference_stg_cloud_service_live_create_e2e]]（POST /api/workspaces/&lt;ws&gt;/services，open-design required=[] 最小 create、trial 不扣款）。

## 後續：console 對外入口缺口 → chart 0.1.2（2026-07-21/22）

E2E 後發現 **console（ttyd web terminal，跑 `claude login` 的地方）沒有租戶對外入口**——只有 ClusterIP，公開 URL 只到 daemon。修法＝auth-proxy 加 `/console` 子路徑代理到 console ttyd（charts#49 issue／charts#50 chart 0.1.2／charts#51 test 修／#401 seed 釘 0.1.2）。連 4 個 PR 全 merged，stg 真部署 smoke（升 my-od）驗 `/console` 全鏈通（ttyd 終端 → daemon shell → claude login）。

**新複用教訓（chart 反代租戶 sidecar 時會重現）**：
1. **NetworkPolicy 擋跨 pod proxy**：auth-proxy(daemon pod)→console(另一 pod) 被 default-deny + Service-CIDR-excluded egress 擋 → nginx `connect() refused` → **CF 502**（不是 timeout，woow-k3s CNI 對 NP-deny 回 REJECT）。修＝NP 明開 daemon↔console:port（pod-scoped allow，ClusterIP 在排除的 Service CIDR 內）。
2. **ConfigMap 改動不 roll pod**：subPath 掛載不熱更新 + configmap 改動不觸發 deployment roll → 升級時 nginx/entrypoint 改動**靜默不生效**。修＝deployment 加 `checksum/config` annotation（sha256sum configmap）。前兩輪 smoke 就卡在 daemon pod 沒 roll。
3. **chart 驗證除真部署 smoke，還要跑 `charts/*/tests/*.sh`**：我 merge charts#50 時漏跑 chart 自己的 render 斷言套件，NP egress 改動撞破 test-networkpolicy 的固定數量斷言（→ test workflow red），事後 charts#51 補。斷言型 test 遇「加規則」要改成不變式（無 internet ipBlock）而非固定 count。
4. ttyd 反代子路徑：ttyd `-b /console`（資產/WS URL）+ nginx `proxy_pass`(no URI 保留原路徑) + `absolute_redirect off`（否則 301 洩漏內部 http://host:8080）。
5. **--reuse-values 忽略新 chart 預設**：升級既有 release 時新增的 chart 預設值（如 console.externalPath）不會自動吃到，要 `--set` 或改傳完整 values；fresh install 不受影響。

## 2026-07-22 stg 上 0.1.2 seed ＋既有服務升級（3 個新複用教訓）

情境：stg 跑 image `20260721.1423`（seed 釘 0.1.1），要讓 chart 0.1.2 的 `/console` 生效。

1. **同版號 seed 變更必須顯式 `-u`**：#398–#401 全在 `18.0.1.0.74` 內（沒 bump 版號），新舊 image 都是 `.74` → Odoo 啟動的版號比對**不會**觸發 re-seed。必須跑 upgrade job（`stg-upgrade-74c`，clone `74b` spec 改 name/image/logfile、`limits.cpu=800m`）強制重載 XML。job image 要用**新** image（0.1.2 的 XML 烤在新 image 的 addon 檔裡）。實證：job Complete 30s → psql `open-design|0.1.2|3`。
2. **升 DB 後「換 image」不可省**：若 running pod 仍是舊 image（seed 0.1.1），一旦 pod 因故重啟且 stg entrypoint 自動 `-u`，會用**舊 image 的 XML 把 DB re-seed 回 0.1.1**（靜默回歸）。正序＝upgrade job 先升 DB → `deploy-stg --image` roll pod。
3. **既有 service 升 chart：`action_redeploy` 的 reuse_values 陷阱**（`cloud_service.py:321` 寫死 `reuse_values=True`）→ chart 新增的**預設值**（`console.externalPath`）不會被吃到，光 redeploy 仍無 `/console`。解＝先把值寫進 `svc.helm_values`（顯式傳的 values 等同 `-f` override，會蓋過 reused），再 `action_redeploy()`。此路優於本機 `helm upgrade`：走 operator 的 helm 3.13.3（避開本機 helm 4 SSA 雷）、平台 DB 同步不漂移、保 PVC。**平台缺口**：platform 沒像 `_inject_allowed_origins` 那樣注入 `console.externalPath`，fresh install 靠 chart 預設沒事，但**升級舊 release 一律漏** → 值得比照 injector 補一個。

**chart 0.1.2 bug → 0.1.3 修復（✅ 2026-07-22 全鏈收官）**：charts issue #52 / PR #53 merged、chart 0.1.3 publish 到 OCI、平台 seed bump PR #402 merged（develop `aebea82b`）、stg 走完 build `20260722.0256`(digest `470368759e`)→ upgrade job `stg-upgrade-74d` → deploy(helm rev 66) → my-od-1 `action_redeploy`(helm rev 3、chart `open-design-0.1.3`)；終驗＝ConfigMap `managed-by=Helm`（smoke 期間的 out-of-band patch 已被官方內容覆蓋歸位）、3 個孤兒程序 restartCount 0→0、`/console` 301 ・`/console/` 200 ・`/` 200 ・`/api/health` 200。原始 bug＝：`console-configmap` 的 `entrypoint.py` 用 `pid,status = os.waitpid(-1, 0)` 等**任何**子程序，會收割 ttyd session 結束後**被 re-parent 到 PID 1 的孫程序**，誤判「被監看服務掛了」→ 全體 SIGTERM → 容器 exit 1。配上 `readinessProbe.initialDelaySeconds=60`，**每關一次終端 `/console` 就 502 ≥60s**。修法＝`supervised = {ttyd_proc.pid, flask_proc.pid}` + `while True` 迴圈，`if pid not in supervised: continue`（孤兒只 reap 不退出，本是 PID 1 職責）。（不影響使用中 session；`claude login` 憑證存在 **daemon pod 的 PVC**、非 console pod，所以重啟不掉登入。）

**PID-1 supervisor 的孤兒收割陷阱是通用模式**：任何「一個容器跑兩個 process 且會 spawn 使用者 session」的 chart（web terminal / shell / exec 型 sidecar）都會踩。**極簡確定性重現法**（不必驅動 WebSocket）：`kubectl exec <pod> -- sh -c '( sleep 3 & )'`——子 shell 立即結束、`sleep` 被 re-parent 到 PID 1，退出時即觸發 `waitpid(-1)`。實測 0.1.2：1 個孤兒 → `restartCount 1→2`；修復後：連續 3 個 → `0→0`。**驗 chart runtime 修復務必做這種 before/after 對照**，只測「修完沒壞」無法證明「真的修好」。

## ⚠️ console 網頁終端「Waiting for open-design pod to be ready...」＝NP 的 apiserver post-DNAT 缺口（2026-07-22 查明）

**更正先前錯誤記憶**：本檔曾記「charts#50 smoke 驗過 ttyd 終端 → daemon shell → claude login」，**該主張不成立**。`/console/` 回 200 只證明 **ttyd 的 index 頁載入**，不證明終端能連上 daemon。實際上 console 終端**從 chart 0.1.0 起就沒通過**，症狀是 ttyd 內無限印 `Waiting for open-design pod to be ready...`（`connect.sh` 找不到 daemon pod）。教訓：**HTTP 200 是「頁面在」，不是「功能可用」**——驗 web terminal 要驗它真的 exec 進去。

**根因**：`connect.sh` 用 `kubectl get pod -l "$POD_LABEL" ... 2>/dev/null`（**吞掉 stderr**）找 daemon pod；console pod 連不到 apiserver → 空字串 → 永遠 Waiting。連不到的原因是 NetworkPolicy：chart 的 `networkPolicy.apiServerCidrs` 預設只有 **`10.43.0.1/32`（kubernetes Service ClusterIP）**，但 **woow-k3s 的 CNI 在 kube-proxy DNAT «之後» 比對 egress**——連 ClusterIP 會被 DNAT 成**真實 endpoint（control-plane node IP:6443）**，不在允許清單 → deny → **`connection refused`**（非 timeout，woow-k3s NP-deny 回 REJECT）。chart values 註解其實已預告此雷（「add the control-plane node endpoint (<nodeIP>/32) if your CNI matches egress post-DNAT」），**是平台從未設定**，性質同 `_inject_allowed_origins`（cluster-specific 值需平台注入）。

**woow-k3s 實際值**：`kubectl -n default get endpoints kubernetes` → `192.168.10.21/22/23:6443`。把這三個 /32 加進 `apiServerCidrs` 後，ClusterIP 與 node IP **兩者同時變通**（ClusterIP 也通正是 post-DNAT 的鐵證），`connect.sh` 的 pod 查詢回傳名稱、`kubectl exec` 成功。

**診斷方法論**：raw socket 測比 kubectl 可靠——`kubectl --server=<ip>` 無憑證時會印「Please enter Username」，**看起來像連上了其實只是互動提示**，差點誤導方向；改用 `python3 socket.connect()` 才拿到乾淨的 `ConnectionRefusedError`。凡 `2>/dev/null` 的健康檢查迴圈，卡住時第一件事就是**把 stderr 打開重跑**。

驗證 `/console` 的訊號判讀：`/console`→**301**＝chart 0.1.2 路由在；`/console/`→401 且 realm=**`ttyd`**＝已穿過 nginx（`auth_basic off` 生效）、是 ttyd 自己的 auth（帳密＝`admin` + `config.sensitive.admin_password`）；若 realm=**`Open Design`** 才是 nginx 預設 location 攔的（＝路由不存在、chart 還是舊版）；**502**＝console pod not-ready（Service endpoint 被摘）。

相關：[[project_mcp_3type_onboarding_375]]、[[reference_stg_db_manual_upgrade_recipe]]、[[reference_stg_cloud_service_live_create_e2e]]、[[feedback_never_fabricate_tool_output]]、[[feedback_teammate_deliverable_verify_objects]]
