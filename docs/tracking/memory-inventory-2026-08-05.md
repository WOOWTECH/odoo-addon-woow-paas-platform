---
title: memory 目錄盤點（224 檔逐檔分類）
date: 2026-08-05
status: 待裁決
---

# memory 目錄盤點 —— 224 檔逐檔分類

盤點日 2026-08-05。**這是提案，尚未執行任何搬移或刪除。**

來源：`~/.claude/projects/<slug>/memory/`（不在 git 裡）。
備份：作業當下 226 檔已複製並逐檔 SHA-256 對帳一致。

## 摘要

| verdict | 檔數 | 意義 |
|---|---|---|
| KEEP_INDEX | 121 | 仍在生效的跨任務教訓／環境事實，留在 memory |
| TO_DOCS | 97 | 已完成的專案紀錄或成體系 recipe，屬 repo 知識 |
| ARCHIVE | 6 | 已過時或已被其他載體取代 |

執行後：單一 MEMORY.md 索引 121 檔（自 224 減 103，-46%）。
現況三個索引檔合計 28,108 字元，每個 session 強制載入。

## TO_DOCS —— 建議搬進 docs/

### → `docs/tracking/`（67 檔）

| slug | 一句話 | 理由 | repo 引用 |
|---|---|---|---|
| `project_269_ha_port_removal_pick` | #269 移除 HA Port 欄位已 merge 收官，含 arm64 原生 e2e 環境 recipe | 已完成專案收官紀錄(issue已關)，其中 recipe 已被更完整版本取代 |  |
| `project_271_internal_mcp_bypass_landing` | #271 內網 MCP bypass 防 regress guard 落地紀錄，PR#308 已 merge | 已收官的 issue landing 紀錄，屬歷史落地知識非跨任務教訓 |  |
| `project_277_278_w1_ns_quota_landing` | W1 P0 ns quota/LimitRange＋孤兒ns reconciler landing與stg驗證 | 已完成專案的上線流水帳（PR#312/#313、stg部署驗證），屬歷史紀錄非跨任務教訓 |  |
| `project_29_389_mixin_sudo_dedup_landing` | #29 mixin sudo ＋ #389 dedup marker 兩張PR收官紀錄 | 已合併PR的落地紀錄，屬歷史專案報告非跨任務教訓 |  |
| `project_317_pvc_keep_landing` | #317 chart-owned PVC 保護收官：install-time post-renderer 修法，PR#321 已 merge 並完成 6 顆 backfill | 已完成且已 merge 的專案落地紀錄，屬歷史知識應併入 docs/tracking（非重犯行為模式） |  |
| `project_318_subscription_unlink_landing` | #318 訂閱刪除聯動+退點結算落地，PR#320 merged 並 stg E2E 驗證，衍生 #324 | 已完成的專案 landing 紀錄，屬 repo 歷史知識而非跨任務可重複教訓 |  |
| `project_324_subscription_dup_charge_incident` | #324訂閱扣款無限重試incident，PR#326已merge、stg清理完成、上游提單 | 已完成的專案事故流水帳（issue怎麼收的），屬repo知識該進docs/tracking |  |
| `project_327_refund_window_landing` | #327 退點視窗改以 next_payment 反推，PR#334 已上 prod | 已收官專案紀錄（merge+prod 上線），屬 repo 知識非長效索引 |  |
| `project_352_allow_policy_bulk_grant` | #352 allow 政策 bulk grant 缺陷修復收官（PR#526 已 merge） | 已完成的 issue 收官流水帳，屬 repo 知識而非跨任務教訓 |  |
| `project_404_apiserver_cidrs_landing` | #404 apiServerCidrs injector 全案收官，stg 通過、prod 待補參數 | 已完成 issue 的收尾流水帳與驗收記錄，屬歷史紀錄非跨任務教訓 |  |
| `project_407_opendesign_dotfiles_persistence` | #407 open-design dotfiles 消失案破案與 PR 收官紀錄 | 已完成的專案收官紀錄，無需再撞的泛用教訓 |  |
| `project_411_reset_values_landing` | #411 reuse→reset values 管線全案 W1–W5 收官，prod 零風險落地 | 已完成專案的完整收官紀錄，5 PR 皆 merged 且已隨版本上 prod |  |
| `project_422_maxpods_backup_chain_closeout` | #422 maxPods 擴容意外挖出 18 家租戶備份鏈六層故障全收官 | 已完成的單日事件收官記錄，載體已在 woow-machines-operator PR |  |
| `project_423_w63_scale_vm_gate` | #486/#488 scale VM 擇一缺陷修復收官，stg 混合拓撲驗證通過 | 已完成上線並在 stg 驗證通過的缺陷修復紀錄，屬歷史 landing 記錄，核心教訓已抽成獨立 feedback 檔 |  |
| `project_423_w6_default_sa_imagepullsecrets` | #423 租戶ns default SA補imagePullSecrets，PR#480交付紀錄 | 已完成PR交付紀錄；內含3點可再用技術事實但已埋在敘事中，隨紀錄一併歸檔 |  |
| `project_423_w6_stg_e2e_round1` | #423 W6 stg e2e 首輪與重跑結果、挖出三缺陷 | 已收官的e2e驗收流水帳，屬歷史測試報告 |  |
| `project_424_item1_hermes_agent_cs_landing` | #424① Hermes Agent CS 化收官：chart 0.1.0+seed 上架，E2E S1-S8 PASS，衍生 #448/#449 | 已完結的產品上線驗收紀錄，屬歷史知識，該進 docs/tracking 而非常駐記憶 |  |
| `project_424_item1_phase_b_seed` | #424 Hermes Agent CS 化平台側 seed 完成(PR#446)，只剩等 chart 0.1.0 發佈做 E2E | 已完成的專案里程碑紀錄，屬單一功能上架歷史 |  |
| `project_424_item2_hermes_agent_image` | #424②hermes-agent OAuth熱修轉build-time image，PR已merge、CI已閉環 | 已完成的映像建置流水帳，一次性交付紀錄，屬docs/tracking |  |
| `project_425_pg_netpol_hotfix` | #425 prod 主 DB NetworkPolicy from-less 收斂，零停機 closed | 已閉環的上線流水帳，issue 已 closed，屬完成專案紀錄 |  |
| `project_433_seed_topology_landing` | #433 seed 拓撲硬編斷言收斂 PR#439 landing 記錄 | 已完成專案收官記錄，核心方法論已另外沉澱進 docs/decisions/patterns |  |
| `project_449_reset_carrier_landing` | #449 密碼重設依載體分流的收官與 stg E2E 驗證紀錄 | 已完成專案落地紀錄，屬 repo 知識非跨任務教訓 |  |
| `project_460_reset_button_gate_landing` | #460 Reset 按鈕 has_admin_gui 三票流水線收官，含 repo-lint 新 gate | 已完成專案收官紀錄，三張 PR 皆 merged |  |
| `project_461_reset_async_landing` | #461 reset 改非同步＋預算階梯，PR#483 已 merge | 已完成專案落地紀錄，設計已另存 docs/plans 與 decisions/patterns |  |
| `project_471_conceal_instances_split` | #471 is_hidden拆conceal_instances/曝光層兩軸，PR#473收官 | 已完成feature split的交付紀錄，含測試/守線/衍生issue，屬歷史落地記錄 |  |
| `project_477_fail_open_family_closeout` | #477 fail-open 家族連鎖PR收官與同族盤點 | 已merge PR鏈的收官紀錄，屬歷史專案報告 |  |
| `project_477_update_service_fail_closed` | #477 修 _update_service 兩個 fail-open 縫，抽出正交兩維度裁決點的設計 | 已完成專案紀錄應進 docs；記錄當時 PR#481 未 merge，但 git log 確認已合併(5f09da26) |  |
| `project_478_marketplace_price_label` | marketplace「Free」價格誤導修正收官，PR#498 merged 並 stg 驗證閉環 | 已完成上線且四方驗證過的專案紀錄，屬歷史存檔 |  |
| `project_482_suspended_redeploy_landing` | #482停權服務redeploy無法復權修復落地，PR#493已merge、A/B實測翻案 | 已完成PR落地紀錄含實測數字，屬「某張issue怎麼收的」tracking類 |  |
| `project_484_named_failure_fields` | #484 operator 具名失敗欄位被忽略兩縫的修復（PR#487） | 特定 PR 修復記錄含可複用教訓，屬專案紀錄非跨任務索引 |  |
| `project_494_hermes_pricing_landing` | Hermes Agent 定價上架 PR#495 收官與 stg 驗收記錄 | 已完成上架 issue 的流水帳，屬一次性歷史紀錄 |  |
| `project_501_global_cap_landing` | #501 HAOS 全域上限收官，含 REPEATABLE READ／SerializationFailure 等技術發現 | 已完成專案收官報告；雖含通用技術發現但整體是單一 PR 的落地紀錄，非可複用方法論獨立檔 |  |
| `project_504_platform_db_backup_landing` | #504 平台主 DB 每日備份落地，CD PR#80 merged，衍生 #512 | 已 closed 的 issue 落地紀錄與踩坑清單，屬 repo 維運知識 |  |
| `project_520_create_service_rollback_landing` | #520 建服務吞例外缺陷修復收官，含半綠驗證與清單不可照抄兩教訓 | 已完結專案紀錄，兩則方法論教訓已各自連結獨立 feedback 檔，本檔適合歸檔為 tracking 歷史紀錄 |  |
| `project_522_identity_anchor_landing` | #522 SA身分錨點改宣告式清單，PR#537已merge收官 | issue已關閉、驗證環境已清除，屬完成的專案紀錄；衍生的通用技術教訓已獨立成reference檔 |  |
| `project_525_a1_skip_trial_rollback_landing` | #525 A1 金流殘留修復PR#527收官＋注入測試技巧 | 已落地PR的技術細節紀錄，屬單一issue收官報告 |  |
| `project_525_a2a3_workspace_invite_rollback` | #525 A2/A3 孤兒 workspace 與已入群卻回失敗修復，PR#540 已 merge，並勘誤 issue 原敘述的 slug 因果 | 已完成且已 merge（git log 確認）的專案紀錄，屬歷史知識 |  |
| `project_529_test_false_green_batch0` | 2198 個測試稽核批次0落地(PR#529)，揭露假綠與「62條從未執行測試」覆蓋盲點 | 測試稽核完成紀錄＋未裁決待辦清單，屬 repo 特定知識非通用教訓 |  |
| `project_530_gateway_landing_542_baseline` | #530 Hermes gateway通道兩側落地＋#542 stg baseline實測與四個假綠陷阱 | 已完成雙PR落地紀錄與一次性實測基準值，屬tracking類專案紀錄 |  |
| `project_559_helm_values_field_acl_landing` | #559 helm_values 欄位繞過 controller 遮罩（層級錯位）修復 | 已覆核完成的修復記錄，PR#569 已定案，屬專案紀錄 |  |
| `project_ar26_sidecar_selfcheck_landing` | ar#26 sidecar 憑證隔離驗證收官，含三個可重用技術發現 | 已 close issue 的收官記錄，技術發現值得留存 docs 供日後查閱 |  |
| `project_ar26_tracka_stg_sidecar_isolation` | ar#26 軌A untrusted skill sidecar 隔離 stg 實機驗證 PASS 記錄 | 已完成的安全驗證測試報告，屬 test-report 類歷史紀錄 |  |
| `project_ar29_operator_redact_closeout` | operator log 洩密兩輪修法與 stg 活體驗證收官 | 已完成 issue 收官敘事，屬 repo 知識非跨任務教訓 |  |
| `project_audit_271_279_280_20260708` | 四路稽核 #271/#279/#280 三態裁定：code/stg 落地、prod 未上線+四個新發現 | 特定時間點的稽核快照，prod 狀態隨後續多輪 rollout 已變化 |  |
| `project_cd77_module_version_gate` | cd#77 版本感知 initContainer 上 prod，取代 SKIP_MODULES_UPDATE 人治 | 尚未完整驗收的持續追蹤項目，適合併入 follow-ups 追蹤檔案 |  |
| `project_docs_audit_20260719` | 2026-07-19 docs全面審查與兩批執行PR收官 | 已完成的docs整理專案紀錄，屬歷史報告 |  |
| `project_e2e_271_279_blocked_rbac_limitranges` | #271+#279 合併 E2E 首跑因 operator RBAC 缺 limitranges 卡關，修復鏈後複測 PASS 全案收口 | 已關閉 issue 的完整 E2E 驗收紀錄，屬歷史知識該進 docs/tracking |  |
| `project_f4231_pods_empty_fail_closed` | F-423-1 容器就緒判定 pods=[] 改 fail-closed，PR#490，方法論=代價收益兩邊重算 | 已拍板落地的專案評估與驗證紀錄 |  |
| `project_ha_chart_build_20260713` | woow-paas-charts新增home-assistant chart並發佈OCI，PR#40已merge | 已完成的chart建置流水帳，一次性交付紀錄，屬docs/tracking |  |
| `project_ha_paas_provision_audit_20260713` | HA PaaS 0元/OAuth/provision 全案稽核與上線收官流水帳 | 14781字龐大已完成上線紀錄，典型「某功能怎麼收的」專案帳 |  |
| `project_ha_woowtech_image_chart_021_landing` | HA PaaS 切 woowtech-ha image＋chart 0.2.1 遷移驗證收官 | 已完成的上線流水帳，chart/image 綁定不變量值得留存 docs |  |
| `project_haos_chart_011_012_landing` | charts/haos 0.1.1（CDI secretRef）＋0.1.2（XFF 剝除）兩版發佈收官 | 已完成版本發佈座標紀錄，屬歷史流水帳 |  |
| `project_k8s_sandbox_plugin_port` | k8s-dev-sandbox 移植成 woow-dev plugin 的完整可行性評估與逐階段實作紀錄 | 已完成大型遷移專案的落地細節，設計文件已存在 docs/plans，此為執行過程補充紀錄 |  |
| `project_mcp_3type_onboarding_375` | 三型 MCP（k8s/vk/opendesign）上架 #375，PR#377 已 merge 且治理鎖已驗 | 完整收官的上架專案紀錄，事實已定案不再變動 |  |
| `project_netpol_279_280_landing` | #279/#280租戶網路隔離epic完整PR序列與e2e驗證紀錄 | epic已全部完成且極詳盡（11869字），是完整landing/測試報告，非日常會再撞的教訓 |  |
| `project_opendesign_cs_onboarding_380` | OpenDesign上架全案收官＋chart連環部署缺陷修復 | 已closed專案的完整除錯史，屬歷史報告非常駐索引 |  |
| `project_prod_canary_smoke_20260708` | prod首租戶canary全鏈煙測PASS_WITH_FINDINGS，人工收尾清理已完成 | 一次性上線里程碑煙測紀錄，已收尾完成，屬「某次上線流水帳」 |  |
| `project_prod_odoo_paas_not_installed` | prod 原未裝 woow_paas_platform，2026-06-26 已補裝上線 | 27天前已解決的一次性首裝事件，屬完成專案紀錄 |  |
| `project_smart_home_mcp_design_20260709` | Smart Home MCP 設計定案＋平台側／HA component 落地紀錄 | 設計本體已在 docs/plans，此檔是後續實作落地記錄，屬完成紀錄 |  |
| `project_subscription_tax_bug_and_selector_redesign_20260718` | 訂閱稅務/trial bug 與方案選擇器重設計全案收官 | 已完成專案紀錄，三張 issue 全部 merged 收官 |  |
| `project_superpowers_docs_promote_20260719` | superpowers 文件批次整理，39 檔全數 promote/archive 出 docs/superpowers | 已完成的一次性文件搬遷專案紀錄 |  |
| `project_wave_a_reseed_landed` | Wave A re-seed三PR收官、與leader3撞車協調共識 | 已完成的seed機制專案落地紀錄，屬歷史報告 |  |
| `project_wave_b_reseed_build` | Wave B re-seed 三張 PR 全數 merged，generator 治理與 M2M noupdate 鐵律經 stg 真升級驗證 | 已完結收官紀錄，可複用機制已另寫入 docs guide，此檔屬歷史驗收記錄該進 docs/tracking |  |
| `reference_act_runner_dind_mtu_custom_network` | act-runner dind 自訂 network MTU 1500 撞 pod MTU 1450，致 CI 下載步驟全紅 | 一次性 CI 基礎設施診斷+修法紀錄，適合歸檔為 pitfall 而非常駐索引 |  |
| `reference_gitea_approval_gate_5xx_orphan_issue` | prod build 審核 gate 遇 Gitea 500 會留孤兒 issue 的故障模式 | 具體單一故障模式記錄，適合併入 docs/tracking/pitfalls 索引體系 |  |
| `reference_hello_world_skill_entrypoint_mismatch` | hello-world demo skill entrypoint 路徑不一致(src/main.py vs scripts/main.py)，#139/140已修復 | 已 RESOLVED 的具體 incident 記錄含完整驗證方法，屬事後歸檔而非仍待撞的通用教訓 |  |
| `reference_odoo_controller_swallowed_exception_still_commits` | Odoo controller 用 except 吞例外回 200，交易仍照常 commit，model 層靠 raise 做 rollback 的守衛整個失效 | 屬 Odoo 框架行為陷阱（MEMORY-odoo.md 同類型），docs/tracking/pitfalls 是既定收納點 |  |

### → `docs/guides/`（13 檔）

| slug | 一句話 | 理由 | repo 引用 |
|---|---|---|---|
| `feedback_shared_cluster_paas_scope_guard` | woow-k3s 共用叢集 namespace 操作範圍鐵律＋CD lane push/merge 人工 gate 分界 | namespace 表已逐字寫入 CLAUDE.md 屬重複；CD lane 段落文中明載尚未進 SOP，應併入部署 guides |  |
| `reference_build_operator_deploy_stg` | paas-operator 建置到部署 stg 全流程 gotcha（tag 觸發失效繞法、Fleet 落點、驗證指令） | 成體系的操作 recipe，適合收進部署類 guides，與既有 deploy-stg/prod-release-sop 文件同類 |  |
| `reference_cross_repo_seed_verify_against_chart_values` | 上架CS template時seed要對chart values.yaml逐鍵核對的三個靜默出錯點 | 成體系的跨repo上架操作recipe，屬「如何做X」該進docs/guides |  |
| `reference_docker_desktop_ci_local_sandbox_image_import` | docker-desktop local 沙盒模式已死，改用 ci-local＋手動匯 image 完整 recipe | 成體系的操作步驟（1-2-3），屬本機測試指南知識非單一教訓 |  |
| `reference_mcp_admin_cs_image_repin` | MCP admin 三服務(odoo/n8n/hermes)映像重釘 rollout recipe，含 CI paths-filter 陷阱 | 成體系的操作 recipe，屬 repo how-to 知識而非通用跨任務教訓 |  |
| `reference_odoo_sandbox_ci_local_two_track_test` | ci-local 沙盒跑 Odoo 全套測試的三個環境坑，及「邊裝邊測」假失敗 vs 兩段式跑法的巨大落差 | 內容自陳需要機械化或寫進 CLAUDE.md/docs 才擋得住重犯，屬成體系操作 recipe 該進 docs/guides |  |
| `reference_odoo_test_docker_desktop_paas_image` | 本機 docker-desktop 用現成 paas-odoo 映像跑模組測試的完整 recipe | 已被 4 份 docs/plans 逐字引用、inbound 19 的核心測試指南，應正式進 docs | ⚠ 有 |
| `reference_security_access_stg_e2e` | SA P1 真 Cloudflare E2E recipe(JSON-RPC driver+四路證據)，發現 stg domain 契約缺口 | 成體系的E2E操作recipe，屬repo測試指南知識，非跨任務通用教訓 |  |
| `reference_stg_cloud_service_live_create_e2e` | stg 建立 cloud service（n8n）完整 E2E 操作 recipe | 成體系的操作步驟（認證/建立/刪除/陷阱），屬測試指南知識 |  |
| `reference_stg_db_manual_upgrade_recipe` | stg dev-sandbox DB 手動升版完整步驟與四大陷阱（image/upgrade job/quota/PVC node） | 成體系的部署操作手冊（含編號步驟與指令），性質同已收斂進 docs/guides/deployment 的 prod-release-sop |  |
| `reference_stg_upgrade_recipe_dev_sandbox` | stg dev-sandbox 升版三步驟操作 recipe 與三方硬驗法 | 成體系操作 recipe 屬 repo 知識，該進 docs/guides |  |
| `reference_tea_cli_write_gotchas` | tea CLI 跨 repo 寫入陷阱大全（arg順序/token/憑證/list截斷等 7 類） | 高度成體系且持續擴充的操作指南，適合轉為正式 tea CLI 使用手冊 |  |
| `reference_woow_playwright_e2e_gotchas` | Playwright測/woow OWL SPA的7個可重用陷阱：click逾時、hash路由、session注入等 | 被2份docs/plans逐字引用(load_bearing)，屬成體系操作recipe，搬進docs/guides時須同步修正wikilink引用 | ⚠ 有 |

### → `docs/reference/`（9 檔）

| slug | 一句話 | 理由 | repo 引用 |
|---|---|---|---|
| `project_275_project_hide_toggle_landing` | #275 Project 顯示開關已上線，prod 預設隱藏、stg 需手動開啟 | 已完成上線的 feature flag 狀態與部署約束，屬 repo 現況知識，適合放進 feature 規格參考文件 |  |
| `reference_bitnami_legacy_image_move` | Bitnami community image 2025-08 起搬到 bitnamilegacy/ 並凍結 | 穩定的第三方 registry 事實，屬 repo 基礎設施規格知識 |  |
| `reference_cf_zero_trust_seat_semantics` | Cloudflare Zero Trust seat計費語意查證定案 | 外部服務規格調查結果，屬事實規格非跨任務錯誤模式 |  |
| `reference_container_upgrade_wakes_suspended_workload` | helm upgrade(reset_values) 會喚醒停權容器(replicas預設1)，VM型因鍵固定而不受影響 | 屬子系統行為機制的規格說明，適合放 docs/reference/features 而非教訓索引 |  |
| `reference_helm_atomic_failure_double_timeout` | helm --atomic 失敗路徑吃 2×timeout，subprocess 未蓋會腰斬 rollback | 綁定 src 程式碼與測試檔的框架行為規格，適合固化為技術參考文件 | ⚠ 有 |
| `reference_hermes_agent_web_dist_patch_constraints` | 改 hermes-agent 前端 bundle 的三個硬約束：路徑寫死、chunk 環狀引用、assets 無 Cache-Control | 綁定單一 vendor 整合的窄範圍技術規格，屬 feature/vendor 參考資料，該進 docs/reference |  |
| `reference_mcp_template_launch_contract` | MCP cloud template啟動指令判別規則(stdio-wrap vs 原生http)＋套件命名衝突陷阱 | 特定子系統的技術規格（是什麼/有什麼），適合進docs/reference供未來上架查閱 |  |
| `reference_odoo_assertraises_primes_cache_masks_accesserror` | assertRaises(UserError) 只在預期 AccessError 時清 cache，會蓋掉真例外 | 穩定的 Odoo 框架行為規格事實，適合併入框架陷阱參考文件 |  |
| `reference_odoo_nonstored_compute_write_silently_dropped` | non-stored compute 欄位 create/write 不拋例外、值直接靜默丟棄 | 穩定的 Odoo 框架行為規格事實，屬技術參考知識非工作方法論 |  |

### → `docs/decisions/`（8 檔）

| slug | 一句話 | 理由 | repo 引用 |
|---|---|---|---|
| `project_427_operator_ingress_baseline` | #427 控制面 ingress 白名單架構論證＋stg/prod 上線紀錄 | 核心是「per-chart egress 結構上不成立」的架構取捨論證，符合決策文件屬性 |  |
| `project_cloud_odoo_addon_selection` | Cloud Service自訂addon選擇的架構決策與跨repo分工 | 純架構決策＋跨repo職責表，屬「為什麼這樣設計」的論述，適合decisions/patterns |  |
| `project_harness_distillation` | harness 蒸餾計畫落地狀態（pattern-guard+repo-lint） | 記錄已完成的架構設計與落地過程，屬決策紀錄 |  |
| `project_meeting_20260710_issue_landing` | 2026-07-10 定價/上線對齊會議 T1–T7 全數轉 issue 並拍板落地的完整紀錄 | 會議衍生的多項架構/定價決策與理由（如不用 Product Combo 當訂閱載體），屬 decisions 類知識 |  |
| `project_pending_label_issue_carrier_20260720` | 訂立「待拍板項一律以 tea issue 為權威載體」的追蹤約定，並清空 note 內累積清單 | 屬工作流程決策紀錄（why + 執行批次），適合併入 docs/decisions 而非常駐記憶 |  |
| `project_pricing_gap_analysis_20260709` | 定價會議定案 vs seed/prod 全面漂移分析，跨月多輪拍板紀錄至 PR#333 上線 | 大量「為什麼這樣定價」的拍板決策紀錄，屬決策文件性質而非跨任務教訓 |  |
| `project_test_suite_zero_red_20260728` | 全套測試收斂到 0F/0E，含三個可複用測試設計判準與兩道新 repo-lint gate | 已完成的測試整頓紀錄，三個判準屬測試設計 pattern 知識，gate 雖已機械化但敘述價值尚未進 docs |  |
| `reference_python_duplicate_method_silently_overrides` | 同 class 內第二次定義同名 method 會靜默覆蓋前一個，針對性測試測不出來只有全套會紅 | 核心防護已機械化進 repo-lint gate，但方法論敘述（針對性測試涵蓋不到）尚未進 docs，值得歸檔為 pattern |  |

## ARCHIVE —— 建議封存（唯一不可逆的一組）

| slug | 一句話 | 理由 | 入度 wiki/md |
|---|---|---|---|
| `project_464_reset_button_visibility_e2e` | #464 Reset 按鈕可見性 E2E 收官，PR#470 五項全過含紅燈對照 | 內容已完整落地於 docs/tracking/test-reports/464-reset-button-visibility-e2e-2026-07-29.md，本檔重複且較舊 | 2/1 |
| `project_492_assertlogs_level_shadow` | assertLogs 字串等級被 Odoo RUNBOT 覆寫成 25 的成因與修復 | 檔案自陳已機械化為 repo-lint 規則＋pitfall doc，內容已被取代重複 | 0/1 |
| `project_ci46_platform_docs_sync` | paas-odoo-ci#46/#50 平台文件同步收官，核心教訓「source 訊號寫成 image 保證」 | 文中明載三行錨點與教訓已落地進 SOP／pitfall 文件，本檔與 docs 重複且較舊，僅 #510 待裁決需另行追蹤 | 0/1 |
| `project_subscription_integration` | 訂閱整合早期架構草案（狀態未合併、含舊版定價方案） | 狀態「尚未合併」與所列定價（n8n/PostgreSQL 等舊價）已被後續大量 subscription 系列 landing 記錄與現行定價取代，明顯過時 | 0/1 |
| `project_wallet_billing` | Wallet & billing dashboard feature開發啟動記錄，Phase 1剛開始 | 114天前的進度快照（Phase 1開始中）明顯已過期，且設計已有docs/plans正式文件承接 | 0/1 |
| `reference_operator_pytest_venv_py311` | paas-operator pytest 須用 repo 內 .venv(py3.11)，系統 python3.9 collection 期即炸 | 內容(含helm 3.13.3陷阱)已完整併入新建的 .claude/skills/paas-operator-dev/SKILL.md，重複且較舊 | 2/1 |

## KEEP_INDEX —— 留在 memory

| slug | 一句話 | 理由 | 入度 wiki/md |
|---|---|---|---|
| `feedback_absence_assertion_needs_its_premise_asserted` | 斷言「無殘留」須連前提「本來會產生殘留」一起斷言，否則恆真 | 跨任務的測試設計方法論，未來寫回歸測試/驗證仍會再犯此坑 | 7/1 |
| `feedback_absence_conclusion_scope_not_universe` | 「查無/零命中/沒被吃掉」結論要先問查詢範圍是否等於全集，含多種變形實例 | 高頻反覆命中的核心方法論、持續擴充，inbound 21 顯示被大量其他 memory 引用，跨任務仍會再撞 | 20/1 |
| `feedback_agent_worktree_stale_base_verify_before_pr` | worktree/分支 base 落後時要驗證，比對用 merge-base 不能用 two-dot | 跨任務會再犯的驗證紀律（inbound=5），2026-08-04 仍在補充新案例，仍在生效 | 4/1 |
| `feedback_answer_landed_but_carrier_unread` | 答案早已落地卻被判「沒回」——驗收要讀對方指定的載體 | 跨任務溝通/驗收教訓，屬會再犯的錯誤模式，非特定專案紀錄 | 0/1 |
| `feedback_apply_your_own_review_standard_to_yourself` | 要求別人做的檢查，回頭套用在自己最有把握、覺得不會錯的那一步時常失守 | inbound=7 高複用行為模式教訓，非單一專案產物，任何驗證/覆核場景都可能再犯 | 6/1 |
| `feedback_assert_the_tests_own_independence` | 測試除守情境前提外，還要守「這條測試相對其他測試是否仍有存在價值」的結構前提 | 跨任務可重複套用的測試設計原則，判準（上游結構事實一改就退化成複本）仍成立 | 0/1 |
| `feedback_ast_gate_single_syntax_form_not_invariant` | AST gate只認一種語法形態，repo內等價寫法（如mapped()）會繞過守線 | 跨任務可再犯的守線設計教訓（下次寫gate都會撞），仍成立 | 9/1 |
| `feedback_comment_env_state_claims_expire` | 註解／驗證結論／memory 宣稱環境現況會過期成假話 | 跨任務教訓，inbound=10 最高群，會反覆再犯非單次事件 | 9/1 |
| `feedback_comparison_experiment_freeze_recipe_not_just_baseline` | 比對實驗要凍結整套 recipe 不只凍結基準；delta 沒有起訖點就沒有意義 | 跨任務可重用方法論教訓，日後任何比對實驗都可能再犯，inbound 4 次被引用 | 3/1 |
| `feedback_completeness_check_output_which_not_how_many` | 完備性檢查要輸出「差在哪」而非「差幾個」的驗證方法論大全 | 跨任務反覆再犯的驗證判準，MEMORY.md 主索引已引用（inbound 13），持續累積新形態 | 12/1 |
| `feedback_declarative_list_gate_needs_floor_folded_in` | 宣告式清單 gate 的假綠洞防範法（floor 折進迴圈） | 通用測試設計原則，是會再犯的假綠模式，inbound 3 | 2/1 |
| `feedback_gate_strength_equals_its_field_write_protection` | 授權閘的安全值＝依賴欄位的最弱寫入保護，示範 #519 role 自助提權繞閘 | 通用安全審查方法論，任何綁欄位的授權判斷都可能再犯，非單一專案紀錄 | 1/1 |
| `feedback_grep_filter_hides_command_failure` | grep 過濾易把指令失敗訊息一併濾掉，查無與查不到長得一樣 | shell 查詢方法論，四種失效形態持續在不同任務被踩到 | 4/1 |
| `feedback_guard_checks_record_not_caller` | 授權守衛檢查記錄屬性而非呼叫者身分＝攻擊者挑對記錄當 self 就整個繞過 | 安全設計反模式，未來新增/覆核授權守衛時仍是同一個易犯錯誤類別 | 0/1 |
| `feedback_identifiers_in_destructive_orders_must_be_looked_up` | 刪除/保留語意指令裡的識別字必須當場查證，不能憑記憶 | 通用不可逆操作紀律，含三種失效形態分析，屬會再犯的錯誤模式 | 0/1 |
| `feedback_interview_confirms_understanding_not_decisions` | 開工前 interview 是確認理解，不是端選單求拍板 | 派工流程規範（含四步流程），持續適用於每張新 issue | 0/1 |
| `feedback_loop_autonomous_scope_discipline` | /loop 自主模式在共用生產 repo 不可自選實質 code 任務，destructive op 成功需同命令內驗證 | 跨任務環境守則，任何自主模式在共用 repo 執行都可能再撞到 | 0/1 |
| `feedback_maestri_dispatch_verify_premise_first` | 派工前、收令後、轉述他人因果分析時都要先驗前提，含多起 2026-08 實例教訓 | 全庫最高頻被引用(18)的核心工作方法論，持續產生新實例佐證 | 17/1 |
| `feedback_negative_verification_needs_positive_control` | 否定式驗證（讀不到/沒發生/只有X壞）必須配陽性對照，否則分不出探針壞掉 | 核心方法論教訓，41處內部引用且持續增補到今日，最活躍的feedback檔 | 40/1 |
| `feedback_never_fabricate_tool_output` | 絕不捏造 tool 輸出，狀態主張須多獨立信號交叉驗 | 核心鐵律，inbound=24 全組最高，反覆被引用驗證 | 23/1 |
| `feedback_parallel_leaders_same_file_collision` | 多 leader 並行撞同一檔案的四種成因、撞車檢查與存活判準 | 高頻協作教訓，inbound 15 次引用，是仍持續發生的協作模式 | 14/1 |
| `feedback_post_hoc_test_lacks_red_phase` | 補的測試沒跑過 RED 階段可能是假守線，含半綠、.pyc 快取等多種變形 | 核心測試方法論教訓，持續有新案例補充（inbound 9），仍是會再犯的模式 | 8/1 |
| `feedback_pr_review_label_default_on` | PR 覆核未完成前標題預帶勿 merge 標示的流程慣例 | 跨情境流程教訓仍在生效，inbound 3 高引用 | 2/1 |
| `feedback_preserve_findings_before_dismiss` | teammate 調查結論須落地到 issue/PR 留言才算保全，dismiss 前必須先保全 | 持續累積5種失效形態的協作流程教訓，跨專案反覆會撞到 | 5/1 |
| `feedback_rollout_workorder_verify_baseline_live` | 開發版單/交接文件的起點事實必須實查，不可沿用 memory 快照 | 每次發版單都會再犯的查證方法論，非單一專案紀錄 | 4/1 |
| `feedback_spec_restates_mechanism_inexactly` | 規格反覆敘述同一機制時各版本不等價，取最完整版而非最嚇人版 | 通用驗證方法論教訓，非單一專案綁定，任何規格驗證場景都可能再犯 | 0/1 |
| `feedback_state_transition_reachability_needs_control_flow` | grep 證明不了「沒有路徑寫成B」，反例常藏在控制流 | 通用驗證方法論教訓，日後查狀態轉移仍會再犯 | 1/1 |
| `feedback_teammate_deliverable_verify_objects` | teammate 交付驗收鐵律：漂亮的完成表格不是證據，必查 git object/remote/PR 實體 | inbound=22 全庫最高複用核心教訓，持續派工/驗收場景都適用 | 21/1 |
| `feedback_test_asserting_absence_locks_shortcut` | 斷言「某查詢不會發生」會把實作省事捷徑寫死成契約，反過來保護缺陷 | 高頻引用(13)的測試設計鐵律，任何新守衛都可能再犯 | 12/1 |
| `feedback_test_pins_transient_fact_as_contract` | 守線測試把當下事實(未上架商品/版號)寫死成契約，例行動作會集體變紅 | 仍是可再犯教訓，且被src/tests測試檔逐字引用，動它會製造懸空引用 | 11/1 |
| `feedback_transcribe_decisions_verbatim_no_extrapolation` | 轉寫 Eugene 決策進 issue/派工不得自行外推語意範圍 | 跨任務教訓，決策轉寫失真仍是會再犯的錯誤模式 | 1/1 |
| `feedback_type_dispatch_needs_subclass_enumeration` | 型別分派驗收要列完整子類樹，不能只核對自己想到的那項 | 跨任務可重用的 code review 方法論，不限單一 issue 才成立 | 0/1 |
| `feedback_verified_once_is_not_continuously_verified` | 驗過一次不等於持續受保護，判準是「改壞它什麼會變紅」 | 通用守線設計原則，與 post_hoc_test 互補，屬會再犯的模式 | 0/1 |
| `feedback_workorder_must_carry_env_landmines` | 工單須寫環境地雷，teammate 新 session 看不到 leader memory | 核心派工教訓，持續更新到 2026-08-05，會再犯 | 1/1 |
| `project_497_stuck_suspended_converge_landing` | 卡死資料收斂會關掉 early-return 保護的唯一修復路徑 | 主教訓是通用 debug 模式，類似結構的 bug 還會再犯 | 1/1 |
| `project_t1_autotopup_behaviour_guard_verification` | T1 自動儲值守衛改行為測試的獨立驗證，A/B 注入證明新守衛真的有守 | 內含可複用的守衛驗證方法論（A/B 注入六要點），未來驗守衛仍會用到 | 2/1 |
| `reference_agent_runtime_llm_key_after_coldstart` | agent-runtime pod的LLM key若在冷啟後才設定永遠不生效，需teardown強制冷啟 | 被docs/tracking/pitfalls與follow-ups-archive逐字引用，load-bearing不可動 | 0/1 |
| `reference_agent_runtime_respawn_mechanism` | agent-runtime 升版強制重生機制＝DELETE reap 非 scale-to-0 | 被 docs/tracking/followups 逐字引用，動它會製造懸空引用 | 4/1 |
| `reference_agent_runtime_stg_verify` | agent-runtime stg 啟用所需 secret 來源與 pod 路徑 E2E 驗證 recipe | 被 docs/tracking/pitfalls/agent-runtime-coldstart-cpu-throttle-timeout.md 逐字引用，load-bearing | 5/1 |
| `reference_agent_shell_git_push_no_tty_tea_token_workaround` | agent shell 無 TTY 導致 git push/fetch 失敗，動態讀 tea token 繞法 | 環境陷阱仍會再撞到，MEMORY.md 發版/CI 索引已引用，非一次性專案紀錄 | 1/1 |
| `reference_audit_411_report_default_overwrite` | audit_411 腳本不帶 --report 會靜默覆寫 repo 報告 | 工具陷阱未修復（尚未實作），會再犯 | 0/1 |
| `reference_bitnami_chart_allowexternal_fromless_np` | bitnami chart allowExternal 預設造成 from-less NetworkPolicy 全開暴露 | 已列於主索引 NetworkPolicy 段，任何新 bitnami chart 部署都可能再撞 | 0/1 |
| `reference_busybox_cp_rn_src_dot_zero_copy` | busybox cp -rn對已存在目的地會exit 0但複製0個檔 | 通用busybox行為陷阱，任何seed/initContainer腳本都可能再犯，非單一issue綁定 | 1/1 |
| `reference_ci_rebuild_same_commit_identical_digest` | 同 commit 重複 build 因 layer cache 全命中會產出相同 digest，並註明此規則的適用邊界 | 發版/CI 通用環境事實，已列在主索引，任何未來發版都可能再撞到，非單一專案產物 | 3/1 |
| `reference_dash_devtcp_false_negative` | 容器 dash 下 /dev/tcp 測連通性會靜默假陰性 | 被 kubevirt 實作計畫引用，環境事實仍會再撞到 | 2/1 |
| `reference_deploy_stg_gotchas` | deploy-stg.sh 多項會中斷部署的環境／互動陷阱與解法 | 被 docs/guides/deployment/prod-release-sop.md 引用，屬 load-bearing 內容 | 3/1 |
| `reference_direct_call_vs_http_rollback_test_false_green` | 直呼 controller/model 寫的 rollback 測試系統性假綠，須走真實端點 | 通用測試方法論教訓，MEMORY.md 主索引「測試與守線設計原則」已列 | 0/1 |
| `reference_distroless_kubectl_no_shell_cronjob` | distroless kubectl image 無 shell 讓 cronjob exit 127 | 被 docs/plans 逐字引用，load-bearing 不可動 | 1/1 |
| `reference_docker_build_local_odoo_test` | k8s-dev-sandbox local-mode 被牆卡住時，用 docker build streaming context 動態跑 Odoo 測試 | repo_refs 非空(被 docs/plans 逐字引用)，且持續有新增補充，屬高頻使用 recipe | 5/1 |
| `reference_dual_session_shared_browser_observation_pollution` | 雙 session 共用瀏覽器/沙盒並發操作，症狀會偽裝成程式 bug（patch 消失、狀態矛盾） | 環境併發陷阱，多 session 協作時仍可能再撞到，屬跨任務環境事實 | 2/1 |
| `reference_elmo_gitea_toypark1234` | 外部協作者Elmo的Gitea帳號toypark1234與查證正解 | 常用查證/派工事實，且含tea CLI誤判陷阱（org members非成員清單）的可再犯教訓 | 0/1 |
| `reference_find_concurrent_file_writer_via_file_history` | 用file-history被刪內容grep反查並行編輯者 | Claude Code工具行為教訓，多session協作時仍會再撞 | 1/1 |
| `reference_fleet_values_go_template_preprocess` | Fleet 在 Helm 之前對 values 檔跑自己的 Go template 前處理，連 YAML 註解都吃，本機 helm template 測不出來 | 通用 K8s/Fleet 環境事實，已列在主索引 Helm/K8s 段落，持續有效且會再撞到 | 1/1 |
| `reference_git_branch_d_checks_upstream_not_develop` | git branch -d／--is-ancestor 判合併會有多種假訊號，正確驗法須先取當前 head.sha | 高度可重複的 git 工作流陷阱(rebase後SHA作廢)，未來每張PR都可能再撞到 | 0/1 |
| `reference_git_extraheader_overrides_url_credentials` | actions/checkout殘留extraheader蓋掉URL憑證，致CI跨repo git操作誤判404 | CI/git環境機制事實＋五輪誤診方法論教訓，跨任務仍會再撞 | 0/1 |
| `reference_git_grep_ere_silent_zero` | git grep -E 的 \s\b\d 退化成字面字母、不報錯 | 工具方言坑跨任務反覆出現，非單次事件記錄 | 1/1 |
| `reference_gitea_single_runner_approval_gate_blocks_ci` | woow-k3s 僅一個 act-runner，prod approval gate 佔用導致全隊 CI 排隊最長 180 分鐘 | 現行基礎設施限制的持續有效事實，索引已引用，仍會再撞到 | 2/1 |
| `reference_ha_trusted_proxies_peer_ip_based` | HA trusted_proxies 判斷看直連 peer IP 非 XFF header 值 | 通用技術機制與驗證陷阱，inbound 4 高度引用 | 3/1 |
| `reference_helm_partial_image_override_digest_merge` | chart 升版新增 image.digest 時 values 只 override 部分欄位會 deep-merge 拼出跨 repo digest 而拉圖失敗 | 已列在 MEMORY.md 現行 Helm/K8s 索引中，屬會再犯的通用 helm deep-merge 陷阱 | 2/1 |
| `reference_helm_resource_policy_stored_manifest` | helm uninstall依install/upgrade時的stored manifest判斷keep，不看live物件 | repo_refs非空，被docs/plans與src/controllers/paas.py實際引用，動它會造成懸空引用 | 2/1 |
| `reference_helm_reuse_values_new_defaults_dropped` | helm --reuse-values 會丟棄新chart的預設值 | 已咬3次的環境陷阱，平台redeploy路徑仍會再撞 | 3/1 |
| `reference_hook_enforced_docs_drift_actively_teach_wrong` | hook 強制讀的 pattern doc 一旦過期會主動教錯，實測29份僅8份乾淨、6份會造成真缺陷 | 已被 MEMORY.md 首段索引引用的持續適用教訓與判斷法(自相矛盾=最強漂移訊號) | 1/1 |
| `reference_k8s_sandbox_test_gotchas` | docker-desktop k8s-dev-sandbox測試多個假綠陷阱（hostPath失效、exit 0假象等） | 高頻引用且更新至兩天前，跨任務環境陷阱仍生效，CLAUDE.md直接點名 | 4/1 |
| `reference_killmode_process_ha_stack_self_restart` | KillMode=process 不保鄰節點 HA 堆疊(repmgr)不自退 | 被 kubevirt 實作計畫引用，叢集節點操作仍會再撞 | 0/1 |
| `reference_kubectl_woow_k3s` | woow-k3s context 透過 Tailscale 連線設定與叢集拓撲細節 | 被 docs/guides/deployment/prod-release-sop.md 引用，屬 load-bearing 內容 | 2/1 |
| `reference_kvm_probe_namespace_scoped` | 免 SSH、免 privileged 查 K8s 節點 KVM／虛擬化能力的技巧 | 可重用的排錯技巧，同類 K8s 參考檔皆留存於 memory 索引中 | 0/1 |
| `reference_longhorn_retain_orphan_semantics` | Longhorn Retain/orphan 語意與 PV 三路徑還原法 | 叢集維運仍在用的機制參考，未過時 | 0/1 |
| `reference_maestri_ask_confirm_first_stalls_teammate` | 派工單要求「先回確認再開始」會讓 teammate 只回確認就停手不執行 | 已列於主索引 maestri 坑清單，協作工具的行為陷阱會反覆重現 | 1/1 |
| `reference_maestri_ask_returns_recipients_screen` | maestri ask 忙碌時回傳對方當下畫面而非回覆，可能是第三方對話內容 | 持續在成長的協作工具操作陷阱，跨任務高頻再犯且仍在更新 | 0/1 |
| `reference_maestri_ask_shell_quoting` | maestri ask 訊息含反引號/$() 會被 zsh 展開，四次連續踩雷後改用 heredoc/檔案傳遞為預設寫法 | 同一坑連續踩四次的高頻工具陷阱，是最需要留在索引提醒的活教訓 | 4/1 |
| `reference_maestri_portal_woow_spa_wedge` | Maestri portal瀏覽器跑/woow SPA會間歇凍結，需agent-browser交叉驗證 | E2E工具環境性故障教訓，未來用Maestri portal測/woow仍會再撞到 | 1/1 |
| `reference_maestri_recruit_name_collision` | maestri recruit撞名會連上既有同名agent | 多agent協作工具持續存在的操作陷阱 | 2/1 |
| `reference_maestri_recruit_resume_pitfall` | maestri recruit 在本 canvas 環境會 resume 舊 session transcript 而非乾淨新開，含止損 SOP 與多次變體 | inbound=7，任何 maestri 協作派工場景都可能再踩，已列在主索引 | 6/1 |
| `reference_meeting_decision_archaeology` | 查產品決策原委的權威來源(會議錄音)與查法 | 跨任務可複用查證技巧，非單次事件，主索引已連結 | 0/1 |
| `reference_memory_files_append_without_conflict_signal` | memory 檔並行 append 無衝突訊號，四條防重複規則 | 本次盤點任務自身即引用的核心 meta 教訓，持續適用 | 0/1 |
| `reference_multi_pr_rebase_merge_verification` | 多 PR 同時發版時 rebase/merge 驗收工具箱（版號比對、merge-tree 三態等） | 高頻引用且是 MEMORY.md 主索引第一條，多 PR 併發場景會反覆用到 | 1/1 |
| `reference_node_maxpods_pending_slots` | 算節點 pod 額度須含 Pending，只算 Running 會低估 | 通用統計方法教訓，容量誤判會再犯 | 1/1 |
| `reference_np_postdnat_apiserver_cidrs` | NetworkPolicy egress ipBlock 匹配 DNAT 後位址，apiServerCidrs 需含全部 control-plane IP | 已列於主索引 NetworkPolicy 段，新環境/新 sandbox 建立時仍會踩到 | 1/1 |
| `reference_odoo_assets_filestore_missing_blank_spa` | 另起容器共用同一 DB 做 UI 驗證時 filestore 不共用，導致 /woow 全白 | Odoo 環境相關的可再犯陷阱，屬 Odoo 框架陷阱類的環境事實 | 3/1 |
| `reference_odoo_call_kw_context_is_client_forgeable` | Odoo call_kw把client context原封塞進env.context，可偽造繞過context守衛 | Odoo框架級安全事實，任何以context key當escape hatch的守衛都會再犯，高度可複用 | 1/1 |
| `reference_odoo_constraint_methods_cache_vs_patched_mock` | patch生效期間首次create觸發constraint cache誤判mock | Odoo框架測試陷阱，寫測試撞patch時機仍會再犯 | 0/1 |
| `reference_odoo_cr_clear_discards_pending_writes` | 探測欄位寫入時 env.cr.clear() 會丟棄未 flush 的 ORM 寫入，讓例外與DB訊號矛盾 | 通用 Odoo 框架陷阱，未來任何 shell 探測寫入權限都可能再撞到 | 3/1 |
| `reference_odoo_empty_string_vs_false_vacuous_assertnotin` | Odoo欄位未設值讀False、seed顯式寫空字串讀''，assertNotIn對後者恆真假綠 | Odoo框架陷阱，寫守線測試時仍會再犯，仍成立 | 2/1 |
| `reference_odoo_filestore_root_owned_gc_loop` | filestore root-owned 檔案讓 Odoo gc 每日無限報錯 | 可複用 Odoo/K8s 維運陷阱＋修法，未來維運仍可能再撞 | 1/1 |
| `reference_odoo_group_system_only_rule_does_not_constrain_group_user` | 只掛 base.group_system 的 ir.rule 對 group_user 完全不生效 | Odoo 框架陷阱，任何寫 ir.rule 授權守線的場合都可能再犯 | 0/1 |
| `reference_odoo_mail_autodelete_unlink_on_invalid_email` | Odoo mail.mail auto_delete 除送達成功外，email 無效/缺漏也會刪信 | Odoo 框架行為事實，含原始碼行號依據，與 MEMORY-odoo.md 家族同性質會再撞到 | 1/1 |
| `reference_odoo_migration_version_window` | Odoo migration 執行條件是版號區間比較非相等 | inbound 6 高度引用的核心框架機制知識 | 5/1 |
| `reference_odoo_never_executed_tests_two_mechanisms` | Odoo 測試「從未執行」兩種機制(未匯入/純unittest.TestCase)與集合差查法 | 通用測試假綠偵測方法論，新增測試檔時隨時可能再犯，inbound=3被多處引用 | 2/1 |
| `reference_odoo_object_button_return_value_is_an_action` | XML button type=object 呼叫的 method 回傳 dict 會被 web client 當成 action 執行而炸掉 | Odoo 框架行為陷阱，改動任何綁 XML button 的 method 回傳型別前都會再撞到 | 0/1 |
| `reference_odoo_plain_unittest_testcase_never_selected` | plain unittest.TestCase在--test-tags下0 tests被選中，靜默不執行 | Odoo測試框架通用陷阱，任何新增測試檔都可能再犯，含具體檢查腳本 | 1/1 |
| `reference_odoo_runner_ignores_expectedfailure` | Odoo test runner不支援expectedFailure、零訊號 | Odoo框架測試陷阱，未來想標記已知缺陷時會再犯 | 0/1 |
| `reference_odoo_search_active_test_hides_inactive` | search() 預設 active_test=True 會靜默排除 inactive record，誤判成「不存在」 | 通用 Odoo ORM 陷阱，高重現機率，且附帶 boolean domain 的姊妹陷阱 | 1/1 |
| `reference_odoo_second_server_dev_assets_e2e` | sandbox pod內另起第二odoo server(--dev=assets)即時載入磁碟改動跑headed E2E | 被docs/plans/2026-Q3實作計畫檔逐字引用，load-bearing不可歸檔 | 1/1 |
| `reference_odoo_session_info_deleted_after_boot` | /woow 前端讀 session 要用 @web/session 非 bare global | OWL 框架陷阱，屬現行程式碼機制，未來開發仍會撞到 | 1/1 |
| `reference_odoo_shell_config_param_no_signal_changes` | odoo shell 改 config_parameter 不觸發 signal_changes 即時生效 | Odoo 框架陷阱，驗證設定是否生效時容易被誤判成缺陷 | 0/1 |
| `reference_odoo_shell_run_testcase_simulated_clock` | 用 odoo shell 程式化跑 TestCase 做多情境模擬時鐘 A/B 對照的技巧 | 被 src/tests/test_cloud_service_deploy_timeout.py 實際引用（repo_refs 非空），且為可重用驗證技巧 | 0/1 |
| `reference_odoo_sudo_getter_cache_priming_test` | 測 sudo getter ACL 要避開 sibling 方法 prime cache 假綠 | 被 docs/plans 逐字引用，load-bearing 不可動 | 5/1 |
| `reference_odoo_test_amd64_woowk3s_cp_overlay` | 本機 arm64 牆繞法——woow-k3s throwaway sandbox+kubectl cp overlay 跑單一模組測試 | repo_refs 非空、inbound 高達9，是持續累積更新的高頻使用 recipe | 8/1 |
| `reference_odoo_test_loader_skips_inherited_methods` | Odoo test loader 預設只收 class 自己的 test method，mixin/繼承寫法會靜默漏測 | 已實際撞過兩種不同形狀（#29、#552）的持續性 Odoo 測試陷阱，仍會再犯 | 4/1 |
| `reference_odoo_two_source_trees_line_numbers_diverge` | 本機兩份Odoo 18源碼（宿主/容器）models.py行號差16 | 環境事實仍存在（兩棵樹都在），引用第三方原始碼行號時會再撞到，且教訓本身是方法論 | 4/1 |
| `reference_openpets_thread_file_multi_session` | openpets thread檔跨session共用會互相覆蓋 | 多session並行操作時仍會撞到的環境事實 | 1/1 |
| `reference_operator_pvc_orphan_label_e2e_recipe` | 驗證 operator PVC orphan-label 路徑生效的隔離 fixture recipe，含 2-release scope 隔離變體 | 已被 docs/guides/deployment/prod-release-sop.md:510 逐字引用（repo_refs 非空），load-bearing 不可搬離 | 0/1 |
| `reference_operator_reuse_values_drops_new_chart_defaults` | helm --reuse-values升級會用舊chart的coalesced values整包蓋掉新chart預設值 | Helm工具本身行為事實，未來任何chart升級都可能再撞，仍成立 | 1/1 |
| `reference_paas_odoo_ci_addon_ref_main_stale` | addon_ref 留空會 clone 移動中的預設分支，已 fail-closed 修掉 | 主索引既有連結，含可複用 CI 驗證判準(驗收看三行) | 3/1 |
| `reference_paas_workspace_namespace_layout` | 一個 workspace 橫跨多個 k8s namespace 的實際佈局事實 | 常被誤推論成一 workspace 一 ns，是仍會再犯的環境事實 | 2/1 |
| `reference_pg_restore_l_false_green_truncated_dump` | pg_restore -l 對截斷 dump 照樣回 0，只有 -f 有鑑別力 | 通用驗證方法論教訓（哪個檢查真的有鑑別力），屬會再犯的檢查陷阱類型 | 2/1 |
| `reference_plan_review_gate_setup` | plan-review gate 武裝狀態與 miniclaude bin +x 陷阱 | 環境設定陷阱在新機器/重裝時可能重演 | 0/1 |
| `reference_prod_odoo_bitnami_module_mgmt` | prod Bitnami odoo chart 的 module 安裝/更新機制、digest別名陷阱、孤兒asset修復法 | prod 維運仍會用到的框架機制知識，但文中「prod DB 零備份」已被 #504 每日備份取代而過期 | 1/1 |
| `reference_prod_values_digest_pin_precedence` | prod values digest pin 優先於 tag，改 tag 是 no-op，回滾須走 git | 內容已制度化進 SOP，此檔現僅作精簡索引指標，留著導航無害 | 3/1 |
| `reference_rebase_silently_eats_manifest_bump` | rebase撞號會靜默吃掉manifest版號bump，需嚴格大於值比對非字串比較 | inbound=10高度被引用，每次PR rebase都可能再犯，且持續在補充新案例(跨百位字串比較) | 9/1 |
| `reference_repo_two_release_trains` | repo內paas-odoo與operator兩條發版線互不相干 | 核心架構事實，寫發版單/盤點版本區間時持續會用到 | 5/1 |
| `reference_sa_suspended_access_sync_gate_orphans_cf_policy` | SA suspended 時刪 policy，CF 同步被 state gate 靜默略過，doomed policy 永久孤兒；核心教訓是因果鏈要驗到發request那一行 | 內含可複用除錯方法論（勿從局部觀察下全稱結論），非僅單一 bug 記錄，跨任務仍會再犯 | 2/1 |
| `reference_session_attribution_transcript_first_message` | 判session歸屬要讀transcript第一則user message，不可信project目錄命名/worktree | 高頻方法論教訓，CLAUDE.md自身「工作方式」索引仍列為優先必讀項 | 2/1 |
| `reference_shallow_clone_no_remote_tracking_ref` | shallow clone push 新分支不會有 remote-tracking ref | git 工具行為陷阱，跨任務驗收仍可能誤判成功為失敗 | 0/1 |
| `reference_tea_actions_runs_list_completed_only` | tea actions runs list 對失敗/進行中 run 顯示失真，須用 API/job層conclusion判斷 | 已列於主索引發版/CI 段，CI 判斷失誤是多次重演的高頻工具坑 | 5/1 |
| `reference_upstream_facts_git_grep_origin_ref` | 查上游現況要用 git grep origin/ref 而非本地工作樹，含比較污染、行號偏移不可推算等延伸教訓 | 被大量引用（inbound 7）且持續擴充的核心方法論，跨任務高頻適用，2026-08-04 仍在新增內容 | 6/1 |
| `reference_virt_launcher_carries_instance_label` | KubeVirt virt-launcher pod繼承VM的instance label，VM型release的pods非空 | K8s/KubeVirt環境事實，糾正舊錯誤假設，任何VM型服務就緒判定/quota計算都會再用到 | 1/1 |
| `reference_woow_k3s_cf_access_egress_ip` | CF Access比對出口公網IP，agent資料面全in-cluster | 被docs/decisions與tracking逐字引用，load-bearing不得動 | 2/1 |
| `reference_woow_k3s_netpol_fresh_pod_race` | woow-k3s NetworkPolicy 控制器正常運作，唯新建 pod 有 1-2 秒啟動編程空窗，測試需用 established pod | 已被 docs/plans/2026-Q3/2026-07-28-haos-kubevirt-v1-implementation.md:331 逐字引用，load-bearing | 3/1 |
| `reference_worktree_edit_tool_unreliable_multiagent` | 多agent並發下.claude/worktrees/內Edit/Read工具不可靠，需Python subprocess寫讀驗證 | 環境工具限制，本團隊多agent並發worktree協作仍常見，教訓仍生效 | 4/1 |
| `reference_zsh_no_word_splitting_verification_script_noop` | zsh 不做 word splitting 讓驗收腳本靜默跑空 | inbound=6 最活躍教訓，2026-08-05 仍在持續更新 | 5/1 |

## 執行前必須一併處理的連結衛生

稽核者發現分類本身完備，但執行計畫缺一道連結步驟：

1. `MEMORY.md` / `MEMORY-landing.md` 有 **6 條 md-link 指向 ARCHIVE 檔**，需同步移除。
2. **4 條 `[[wiki-link]]` 指向 ARCHIVE 檔**，其中 `reference_odoo_test_amd64_woowk3s_cp_overlay.md:43` 的引用者本身是 KEEP_INDEX，會留下永久懸空 → 建議改指 `.claude/skills/paas-operator-dev/SKILL.md`。
3. **3 個 TO_DOCS slug 被 repo 逐字引用**，搬檔後要同步改路徑：
   - `reference_helm_atomic_failure_double_timeout` ← `src/models/cloud_service.py`、`src/tests/test_cloud_service_deploy_timeout.py`（**程式碼與測試內**）
   - `reference_odoo_test_docker_desktop_paas_image` ← 4 份 `docs/plans/`
   - `reference_woow_playwright_e2e_gotchas` ← 2 份 `docs/plans/`

## 既有的懸空引用（本次盤點順帶發現，與上述提案無關）

2026-08-04 刪除 9 份 `prod_rollout` 記錄時未清理入度，盤點當下仍有 **11 處懸空 wiki-link**
（slug 前綴刻意寫成 `pr_` 以免被連結改寫腳本再次誤傷本段——實際 slug 是 `project_prod_rollout_*`）：

| 已刪除的 slug | 盤點時的入度 | 本次處置 |
|---|---|---|
| `pr_20260731_88` | 4 處 | ✅ 已改指 `docs/guides/deployment/prod-release-sop.md` |
| `pr_20260720_73` | 3 處 | ✅ 同上 |
| `pr_20260725_78` | 2 處 | ✅ 同上 |
| `pr_20260728_80` | 1 處 | ✅ 同上 |
| `pr_20260711_53` | 1 處 | ✅ 同上 |

實際修了 **17 處**（memory 側 8 處 + 已搬進 docs 的引用者 9 處），高於盤點時算到的 11——
因為盤點只掃 memory 目錄，沒把「引用者自己也會被搬進 docs」算進去。

### 仍未處理（刻意）

| 位置 | 引用的已刪 slug | 為何不改 |
|---|---|---|
| `docs/plans/2026-Q3/2026-07-28-haos-kubevirt-v1-implementation.md:515,689` | `pr_20260708` | plans 是歷史計畫紀錄；且該行已把教訓（「digest 必自驗」）寫在原地，引用只是出處 |
| `docs/guides/deployment/prod-release-sop.md:114` | `pr_20260710_51` | 同上，SOP 本身已是該筆紀錄的承接者 |
| `docs/archived/superpowers/plans/2026-07-10-330-pricing-seed-redesign-plan.md` | `reference_odoo_test_docker_desktop_paas_image` | 封存檔＝歷史紀錄，改它等於竄改當時事實 |

另有 55 種**命名慣例漂移**（連結用連字號或省略 type 前綴，檔案其實存在），未處理——
它們指得到東西，只是形式不一致，風險與修改成本不成比例。

### 這次盤點暴露的一條方法論

「修懸空連結」的腳本會**把記錄該問題的文件一起改掉**——本段就被誤傷過一次（`git diff` 才看出來）。
凡是「掃全庫替換」的修復，**記錄問題的文件必須排除在替換範圍外**，否則問題的證據會被它的修法覆蓋。
