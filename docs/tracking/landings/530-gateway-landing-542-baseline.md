# project 530 gateway landing 542 baseline

> 自 Claude memory 遷移（2026-08-05）。原 slug `project_530_gateway_landing_542_baseline`。盤點依據見 [memory-inventory-2026-08-05](../memory-inventory-2026-08-05.md)。
>
> 原 description："#530 Hermes gateway 通道兩側落地（PR#539/charts#69）＋#542 三態實打的 stg baseline 實測值與四個假綠陷阱"

2026-08-02～03 落地。**問題**：租戶部署 Hermes Agent＋Hermes MCP 後，MCP 的 Gateway API 區永遠 Disconnected。**兩個成因**：(a) gateway 跑在容器 8642，但 CS 對外只有 authproxy:8080，nginx 完全沒有指向 8642 的 location；跨 ns 直連被租戶隔離 NetPol 擋（實測 connection refused，同 ns 對照組可達）。(b) API key 由 chart 自產、平台從未讀取，租戶拿不到值。

**影響面實測**：MCP 的 10 個 tool 只有 2 個依賴 gateway（`hermes_chat` 打 `/v1/responses`、`hermes_inspect(capabilities)` 打 `/v1/capabilities`），其餘 8 個走 Dashboard REST API ⇒ **dashboard 填對就能用 8 成**。dashboard_url **在 0.1.x 當時**必須帶 `/dashboard` 尾綴（nginx `/dashboard/` → `127.0.0.1:9119/`），填服務根 URL 會打到 WebUI；**0.1.3 起正好相反**——webui container 移除、nginx `/` 直連 dashboard，dashboard_url 必須是**服務根 URL、不帶任何子路徑**，帶 `/dashboard` 會打到硬 404（見下方 2026-08-10 補記）。⚠ 這「8 成靠 dashboard」正是 #672 的爆炸半徑：平台從未交付 dashboard URL 這一格，租戶手上三格缺一格＝管理面整組失效。

> **2026-08-10 補（chart 0.1.3，feat/hermes-agent-013-cleanup）**：hermes-agent 已
> 移除 dashboard subpath。0.1.3 起 dashboard 走 root URL（nginx `/` 直連 dashboard
> 容器，webui container 一併移除），OAuth `redirect_uri` 與 dashboard REST API 都
> 改吃 root URL；`/dashboard` 尾綴會打到 404 subpath。詳見 feat/hermes-agent-013-cleanup
> 與 scripts/audit/repo-lint.py 的 `hermes-agent-013-seed` 守線。

## 落地內容

- **charts PR#69 → hermes-agent 0.1.1**：`gateway.externalPath`（預設 `/gateway`）＋ helper 正規化（照 `dashboardPath` 模式，非裸內插）＋ authproxy 兩個 location（`= /gateway` 301、`/gateway/` → `127.0.0.1:8642/`），`auth_basic off`＋`proxy_buffering off`＋3600s。**不動主 Service**（裸 fullname/8080/targetPort=proxy 是平台 invariant，chart 端 `tests/test-service-invariant.sh` 硬斷言）。
- **平台 PR#539 → develop（manifest .97）**：seed `gateway.externalPath`、`helm_chart_version` 0.1.0→0.1.1、`cloud_service_secret.gateway_api_key` 欄位、走既有 sensitive 分離通道送 `auth.apiServerKey`、一次性交付（create/reset response，**不新增回讀端點**）、`OneTimeCredentialsModal`（多具名憑證各自複製）。
- **chart 端零改動即可接受平台供給**：`auth.apiServerKey` 鍵**本來就存在**（values.yaml:269），憑證解析三段優先序＝顯式 values → lookup 既有 Secret → randAlphaNum 64。

## 三個 code review finding（同一修法一次解，chart 端零配合）

交付判準原本是手維護 `path_map={'hermes-agent':'/gateway'}`，改為**讀 template seed 的 `gateway.externalPath`**，鑄造／交付／前端文案三處同源。原缺陷：前端文案 gate 用 `t-if="hasAdminGui"`，但 `has_admin_gui=True` 有 **8 個 template**（實測，DB registry 與 grep XML 雙查法一致）⇒ 其中 7 個租戶被告知一件永遠不會發生的事。

⚠ **無 key 時整個 `auth` 鍵不送、不是送空字串**——chart 第一優先序是顯式 values，送空值會蓋掉 chart 自產、agent 正在用的那把 → gateway 當場全拒。

## 🔴 #542 三態實打的四個假綠／假紅陷阱

1. **健康端點回應形狀相似但來源不同**：外部打 `/health` 拿到的是 **WebUI(8787)** 的 `{"status":"ok","sessions":0,...}`，gateway(8642) 真正的是 `{"status":"ok","platform":"hermes-agent","version":"0.19.0"}`。**必須用需 Bearer 的 `/v1/capabilities` 驗，不能用健康端點。**
2. **urllib 預設 UA 被 CF 擋**：`Python-urllib/3.x` → `error code: 1010`；**httpx 不會**。用被擋的 client 驗＝假紅，會誤判成通道沒開。
3. **gateway 忽略 `X-Forwarded-Prefix`**（實測：帶與不帶回應逐位元組相同 2668 bytes、回應內無 `/gateway` 字樣，配陽性對照＋錯 token 鑑別力對照）⇒ chart 不送該 header 是對的。
4. **dashboard 前端 JS 沒帶 prefix**：`POST /auth/password-login` → 401/302；`POST /dashboard/auth/password-login` → 200（payload 需 `{"provider":"basic","username","password"}`，缺 provider 回 422）。

**Bearer 是 fail-closed**（實測：無 token 401／錯 token 401／對 token 200），故 `auth_basic off` 安全。

## #542 baseline（2026-08-03 08:xx 實測，全唯讀同時段）

| 項目 | 值 |
| --- | --- |
| stg addon 版號 | **18.0.1.0.88**（DB `woow_stg`，installed）— develop 已 .97 |
| hermes-agent release | `svc-bd9d6ed3` / ns `paas-ws-stg-5a105e8b`，chart **0.1.0**，**revision 2**（Aug 2 13:09 `Upgrade complete`，作者不明） |
| `API_SERVER_KEY` | 仍在，64 字元（另 admin_password 32、dashboard_session_secret 64） |
| ResourceQuota | `limits.cpu` **21**（used 9350m）、memory 36Gi、**PVC 10/10 已滿** |
| 0.1.0 vs 0.1.1 | `helm template` 物件種類與數量**完全相同**，不新增 PVC ⇒ PVC 滿不擋 upgrade |
| ns 內 release | 4 個：open-design 0.1.4、n8n 2.0.1、hermes-agent 0.1.0、haos 0.1.2（**quota 是 ns 級共用**） |
| 使用中？ | authproxy log 近 2h **0 筆**、近 24h 42 筆（陽性對照：管道正常），最後活動 Aug 2 21:54 UTC |

**⇒ A/B 兩層可拆**：
- **A（通道驗證）零破壞**：`helm upgrade` 0.1.0→0.1.1 ＋ 用既有 `API_SERVER_KEY` 打三態。**不需 Reset、不需升平台、不動 admin_password**——因為 gateway 走 `auth.apiServerKey` Bearer，與 `config.sensitive.admin_password`（webui/console/dashboard/ttyd 四入口）是獨立憑證。
- **B（端到端，MCP Test Connection 綠）**：需先把 stg 從 .88 升到 ≥.97，否則平台側**根本沒有**生成/交付 gateway key 的邏輯，Reset 也不會換發 ⇒ 失敗形狀會長得像「chart 的 gateway 路由沒生效」（**探針失敗被讀成被測物失敗**）。

⚠ 未驗：stg 平台（.88，DB 內 template 仍 0.1.0）的 reconcile 會不會把手動 upgrade 的 0.1.1 打回。

## 衍生單

`#542`（本收官）、`#535`（priority:high，租戶憑證明文從服務詳情 API 外洩，6 欄位/4 template 含租戶 Odoo admin 密碼，前端一律顯示 `********` 故**畫面看不出來、洩漏面在 API 層**）、`#544`（另 47 條測試從未執行，與 `#467` 不同機制）。

相關：[[reference_mcp_template_launch_contract]]、[[reference_paas_workspace_namespace_layout]]、[[project_424_item2_hermes_agent_image]]、[[reference_stg_upgrade_recipe_dev_sandbox]]
