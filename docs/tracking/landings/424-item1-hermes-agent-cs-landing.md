# project 424 item1 hermes agent cs landing

> 自 Claude memory 遷移（2026-08-05）。原 slug `project_424_item1_hermes_agent_cs_landing`。盤點依據見 [memory-inventory-2026-08-05](../memory-inventory-2026-08-05.md)。
>
> 原 description："#424① Hermes Agent CS 化收官：chart 0.1.0＋seed PR#446（.80，hidden 待定價）；E2E S1-S8 PASS；衍生 #448/#449；apiServerCidrs 四筆與 post-DNAT 教訓"

2026-07-28 #424① 收官。**chart `hermes-agent 0.1.0`**（woow-paas-charts PR #58，Bellows）＋ **seed PR #446**（develop `448e5714`、`18.0.1.0.80`，Dovetail）。`cloud_app_hermes_agent`（2026-07-28 當時）以 **is_hidden=True 不掛 plan** 上架（不可購買；定價拍板後比照 #431 解封，勿與 `cloud_app_mcp_hermes`／slug `mcp-hermes` 混淆——不同產品；**2026-07-31 起已依 #494 正式解封，見下方「stg 臨時解封」節末的更新**）。

## 架構落點

- 一 pod 三容器（agent＋webui＋nginx auth-proxy :8080 唯一對外）＋獨立 console（ttyd，open-design pattern）；DB/Redis **不需要**（實測 hermes_cli 用 sqlite，Elmo compose 的 pg/redis 是空掛）。
- image 三顆全雙釘：agent `20260727.1502@ce3c49a7…`（②產物）、webui `20260727.2228@98d48222…`（mirror-image workflow 首筆；**釘 per-arch amd64 child 非 index**——open-design-console 同慣例）、console 沿用 open-design-console。
- 單一 admin_password 餵四入口；dashboard 走 `/dashboard/` subpath（X-Forwarded-Prefix 原生支援）；`HERMES_DASHBOARD_PUBLIC_URL` per-service 注入且**必含 /dashboard 前綴**（OAuth redirect_uri 權威源）。
- Elmo pilot 的叢集全域 ClusterRole、明文密碼 env、cloudflared per-instance tunnel 全數不搬。

## E2E（stg 真叢集，S1–S9）

S1–S8 PASS（quota used 8832Mi=8.625Gi 實地印證測試算式；深健康 `/api/health`＋8642；console exec；三路由；PVC orphan-label 全鏈）。**S9 密碼重設紅=operator 既有缺陷**（非本 PR）：

- **#448（high）**：`upgrade_release` 缺 `imagePullSecrets` setdefault（create 有 `releases.py:63-65`、upgrade `:151` 沒有）→ #411 reset 語意下**任何私有 image CS redeploy 即掉 pull secret**（open-design 沒炸只因未 redeploy 過）。
- **#449（medium）**：reset-config saga 寫死 mcp-server 的 `/data/config.json` 佈局 → hermes-agent 密碼重設 500；fail-safe 已驗（rollback、舊密碼可登入、不鎖死）。

## 三個教訓（詳 [[reference_np_postdnat_apiserver_cidrs]]）

1. NP egress ipBlock 是 **post-DNAT 匹配**——apiServerCidrs 只塞 ClusterIP 時 console 全癱；正解＝ClusterIP＋全部 control-plane node IP。
2. 「#404 stg E2E PASS ⇒ ClusterIP-only 可行」是誤推——stg 會過是因為 ops param `woow_paas_platform.apiserver_cidrs` 已設四筆。新環境漏設＝fallback 單筆＝console 癱。
3. **pod Ready 不可信**：gateway 死時 pod 照樣 3/3 Ready（缺 fsGroup＋跨 uid 寫入案例）；驗收要深健康信號。另 `API_SERVER_KEY` <16 字元會靜默不開 8642。

**#448 已閉環（2026-07-28 同日）**：PR #450（`_ensure_image_pull_secrets` helper，create/upgrade 同源；unit 398＋integration 23 passed＋紅燈驗證）→ 自動 build run 1213（push 到 develop 動 `extra/paas-operator/**` 即觸發，tag=短 SHA `8c11a77`）→ CD PR #63（stg）/#64（prod）Eugene 親按 → 雙環境 digest `a69cfb3e…` 比對＋health＋**stg 產品路徑實證**（open-design `my-od-2` redeploy 後 pull secret 保留、helm v2→v3）。CD pin 慣例＝PR 標題帶「⛔ 勿代 merge｜待 Eugene 親按」。#449（reset saga 設計題）仍 open，內含 reset_release_config 的同款 latent gap 註記。

#424 殘餘：③ VK host OpenChamber（後批）、Elmo 一題確認、定價後解封。①②③中的 ①② 閉環。

## stg 臨時解封（2026-07-28，DB 直改非 code）

Eugene 要親測，stg DB（woow-k3s `dev-sandbox-stg` ns、pod stg-odoo-dev-sandbox-odoo、db `woow_stg`——**stg.woowtech.io 的 Odoo 在 dev-sandbox-stg 不在 paas-stg-system**）直改：template 46 `is_hidden=False`＋新建 0 元測試 plan **id 268**（copy 自 plan_mcp_hermes_monthly；1c/3G/5G、月繳、sh_trial=False、storefront_hidden=False）掛上 M2M。已獨立唯讀驗證。（2026-07-28 當時預測，07-29 已驗證成立一次：）**下次 stg 部署 `-u` 會把 is_hidden 翻回 True**（templates seed noupdate=0）；plan 268＋link 不受影響（hermes-agent 在 links seed 無 (6,0) 記錄）→ 重翻一行即可。正式解封仍待定價，比照 #431（seed＋migration 雙軌）。

07-29 #449 F-449-1 升版 `-u` 如預期翻回 → **已重解封**（BEFORE True→AFTER hits=1；plan 268/link 實測未受 -u 影響，驗證了「重翻一行即可」）。svc60 密碼經 reset E2E 輪替，DB cloud_service_secret 與 ns Secret 一致。（**此後這條「每次 stg 升版重複翻一行」的敘述已被下方 2026-07-31 起的正式解封取代**——seed 永久改為 False 後 `-u` 已無 True 值可翻，無需再手動重翻。）

**2026-07-31 起已正式解封（#494，commit `fa10bccd`）**：`cloud_app_hermes_agent` 的 `is_hidden` 在 seed 永久改為 `eval="False"`（非本節 07-28 的臨時 DB 直改），定價 4,500/36,000＋7 天 trial 依 #494 拍板上架。**上一段「下次 `-u` 會翻回 True」自此不再成立**——seed 本身已無 `True` 值可翻，`-u` 只是 idempotent 套用既有的 `False`。2026-08-10 查證（#653）：seed（`src/data/cloud_app_templates.xml` 的 `cloud_app_hermes_agent`）`is_hidden` 仍為 `False`；stg 升版 `.119`→`.121` 跑完 `-u` 後，DB `is_hidden` 查詢結果為 `f`，與 seed 一致、未被翻回。

UI E2E（07-28/29 全鏈 PASS）：marketplace 卡片→configure（plan 自動選取）→Launch 建立→pods 3/3+1/1→外部路由（無憑證 401、webui/dashboard 登入頁 200、console 200、`/api/health` ok）→列表/詳情頁（ONLINE、chart 0.1.0）→刪除清理無殘留。存活服務＝svc 60（`svc-bcb0aa5b`，https://paas-cs-5a105e8b-532079c5.woowtech.io，密碼在 ns secret `svc-bcb0aa5b-hermes-agent-secrets` 的 admin_password）。**衍生 #456**：plan cs_vcpu=1 的 quota 邊際 +3 < 主 pod limits 3.4 CPU → 共用 workspace 第 1 筆永久卡 0/1 而 DB/UI 照樣 running（空 workspace 靠 8 vCPU floor 蓋住、S1-S8 測不到）；正式 plan cs_vcpu 須 ≥2 或 min_vcpu 改 ÷3 上取公式。

關聯 [[project_424_item2_hermes_agent_image]]、[[reference_hermes_agent_web_dist_patch_constraints]]、[[project_433_seed_topology_landing]]。

## 0.1.3 transition（2026-08-10，feat/hermes-agent-013-cleanup）

chart `hermes-agent 0.1.3`（woow-paas-charts 移除 webui container，nginx `/` 直連
dashboard）上線需平台端同動：

**contract change**：`_inject_dashboard_public_url` 對 hermes-agent slug 由
``https://{sub}.{domain}/dashboard`` 改為 ``https://{sub}.{domain}``（root URL）。
**`HERMES_DASHBOARD_PUBLIC_URL` 從 `/dashboard` 變 root URL**——MCP Server /
OAuth client 須重新註冊 callback URL 到 root path，**升版前必須先告知所有
hermes-agent 租戶**。

**seed 同步**：
- `helm_chart_version` 0.1.1 → 0.1.3
- `helm_default_values.webui` block 整個移除
- `helm_default_values.dashboard.externalPath` 移除

**SPA 0.19.0 不動**——SPA fetch bug 由 chart 0.1.3 把 dashboard 移到 root 自動修復，
無需改 image。

**rollback SOP**：若需從 0.1.3 回退到 0.1.x，須同動 (a) chart version pin (b) 加回
`webui` block 與 `dashboard.externalPath` (c) `_inject_dashboard_public_url` 邏輯
反轉——三件必須同 PR、否則 OAuth redirect_uri 立刻 404。三項分別在 seed 與
cloud_service.py，**不可只翻一個**。

**公告窗口**：MCP server 須事先重新註冊 callback URL（root）才能升版；窗口最短
應 ≥24h（給 OAuth client 端的 cache TTL 與輪換時間）。本工單由 caller 在升版當天
公告，發版單（`/woow:deploy`）附上此 SOP 連結。

**守線**：scripts/audit/repo-lint.py 的 `hermes-agent-013-seed`（檔頭已登錄）；
test_hermes_agent_seed_424 的 `test_dashboard_gates_explicit` /
`test_dashboard_external_path_matches_injector` /
`test_inject_dashboard_public_url` 三條機械斷言。

**跨 repo 順序**：必須等 chart 0.1.3 PR 合併 + 在 OCI registry 驗證可 `helm pull`
才合併本 PR；另一個 teammate 開 chart PR（在 woow-paas-charts repo，
target=main），本 PR 在 odoo-addons/woow_paas_platform（target=develop）。
chart 先 merge → 幾分鐘後驗證 → 平台才能 merge。
