# project 424 item1 phase b seed

> 自 Claude memory 遷移（2026-08-05）。原 slug `project_424_item1_phase_b_seed`。盤點依據見 [memory-inventory-2026-08-05](../memory-inventory-2026-08-05.md)。
>
> 原 description："#424① Hermes Agent CS 化 Phase B（平台側 seed＋注入接線）＝PR #446 open、全 suite 回 baseline；只剩 E2E 等 chart 0.1.0 發佈"

2026-07-27/28 完成 #424 第①項的**平台側（Phase B）**，PR **#446**
（odoo-addons/woow_paas_platform，`feat/hermes-agent-cs-seed` → `develop`，Refs #424）。
Phase A（chart，woow-paas-charts）由 teammate Bellows 平行進行。

**已落地**：seed `cloud_app_hermes_agent`（hidden＋不掛 plan＝不可購買，定價拍板後比照
#431 解封）、`CloudService._inject_dashboard_public_url` 新 injector（per-service 注入
`config.dashboardPublicUrl`＝MCP OAuth 回呼前提）、`_inject_apiserver_cidrs` /
`_inject_storage_class` 加 `hermes-agent` slug、40 條守線測試、manifest .80、F-424-1
follow-up。四顆 image 全釘（agent／webui／console digest 皆向 registry 直驗，authProxy
sidecar 依 chart 要求釘 tag 級）。全 suite 3F/2E ＝ develop baseline，零回歸。

**只剩一項阻塞**：dev sandbox E2E 要 chart `hermes-agent:0.1.0` 發佈（等 charts PR #58
merge → publish-charts 自動發佈）。腳本已備在 scratchpad（odoo shell 直建，**不落 repo**
避免變成第三套 provision）。**E2E 必須在 stg/woow-k3s 跑**：本機 docker-desktop 是
Apple Silicon arm64，而 agent image 是單一 amd64 manifest、webui 釘 amd64 子 manifest。

**契約教訓（跨 repo seed 對 chart）**：光靠派工文件的 values 契約摘要不夠——直接 fetch
chart 的 `values.yaml` 逐鍵核對，抓到摘要沒寫的 3 個缺鍵（`authProxy.image` /
`workerProcesses` / `dashboard.bindHost`），其中 bindHost 是
安全閘位（loopback 會靜默關掉上游 fail-closed auth gate）。做法見
[[reference_cross_repo_seed_verify_against_chart_values]]。

> **2026-08-10 補（chart 0.1.3，feat/hermes-agent-013-cleanup）**：原第 4 個
> `dashboard.externalPath` 在 0.1.3 起從 seed 移除——dashboard 改走 root URL（nginx `/`
> 直連），OAuth `redirect_uri` 由平台 `_inject_dashboard_public_url` 注入 root URL。
> 留著 `externalPath` 會被 chart 當成 subpath 轉發，與平台注入對不起來（OAuth 回呼
> 打到 404 subpath）。守線＝scripts/audit/repo-lint.py 的 `hermes-agent-013-seed`
> 與 test_hermes_agent_seed_424 的 `test_dashboard_gates_explicit` /
> `test_dashboard_external_path_matches_injector` /
> `test_inject_dashboard_public_url` 三條。

**挖到的地雷（已寫進 seed 註解）**：`allocated_vcpu/ram_gb/storage_gb` 三欄無預設值，
ns quota＝`sum(allocated_ram_gb)×3` 且 floor 只有 8Gi < hermes 全容器 limits 8.625Gi。
marketplace 路徑帶 `_allocated_vals_from`，但 `ha_paas.py` 的 provision **沒帶**
（HA 只吃 1Gi、floor 蓋得住）——將來照抄 HA 那條寫 hermes provision 端點就 pod
FailedCreate。參見 [[project_opendesign_cs_onboarding_380]]、
[[project_424_item2_hermes_agent_image]]。
