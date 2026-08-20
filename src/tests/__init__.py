# Test suite for woow_paas_platform
from . import test_pricing_seed_330
from . import test_smart_home_mcp
from . import test_account_move_pay_from_wallet
from . import test_account_move_skip_hook_field
from . import test_action_post_hook_isolation
from . import test_cloud_app_template
from . import test_cloud_service
from . import test_cloud_api
from . import test_paas_operator
from . import test_smart_home
from . import test_workspace_cascade_cleanup
from . import test_smart_home_template
from . import test_security_access_template
from . import test_security_access
from . import test_security_access_lifecycle
from . import test_security_access_subscription_flow
from . import test_security_access_endpoints
from . import test_smart_home_subscription_flow
from . import test_oauth2
from . import test_oauth2_device_flow
from . import test_ha_api
from . import test_ha_api_security_access
from . import test_naming
# feat: L4 reconciler 左集蒐集（SA 2 層 walk + CS MCP 重建 + SM）
from . import test_reconciler_left_set
# feat: L4 reconciler Odoo 端 delete-gate（reconcile 路徑零刪除機械守線）
from . import test_reconcile_delete_gate
# feat: L4 reconciler 差集引擎（右集 − 左集）+ inventory client + 端到端
from . import test_reconciler_diff
# feat: L4 reconciler env-pin self-check + 両端一致 + shared_tunnel_id 必填（TS-4 S2）
from . import test_reconciler_env_pin
# feat: L4 reconciler 分類層 — env attribution + 結構 allowlist + protected + tripwire（TS-4 S3+S4）
from . import test_reconciler_classify
# feat: L4 reconciler Phase D 報表 model — snapshot 持久化 + admin-only ACL（TS-5 Slice A）
from . import test_reconcile_report
# feat: L4 reconciler Phase D 報表 endpoints — admin-only + no-silent-drop + reverify（TS-5 Slice C）
from . import test_reconcile_report_endpoint
# feat: #277 L4 reconciler namespace 左集差集（CS∪agent union + kind 對映 + ns env 分類）
from . import test_reconciler_ns
# feat: L3 deploy_env CF naming env token（config_parameter 接線 + 卸載清理）
from . import test_deploy_env_config
from . import test_mcp_integration
from . import test_subscription_lifecycle
from . import test_low_balance_notification
from . import test_payment_methods_api
from . import test_profile_api
from . import test_wallet_topup_auto
from . import test_wallet_topup_model
from . import test_payment_transaction_fields
from . import test_payment_token_charge
from . import test_wallet_topup_classify_failure
from . import test_wallet_topup_invoice
from . import test_wallet_topup_payment_hooks
from . import test_payment_transaction_overrides
from . import test_topup_controller
from . import test_topup_status
from . import test_topup_cron
from . import test_change_plan_refund
from . import test_subscription_failure_handling
from . import test_topup_plan_best_value
from . import test_skip_trial_estimate
from . import test_wallet_transaction_bonus
from . import test_dashboard_summary
from . import test_ecpay_invoice_carrier_mapping
from . import test_ecpay_invoice_cron_force_issue
from . import test_ecpay_invoice_topup
from . import test_ecpay_invoice_retry
from . import test_ecpay_invoice_subscription
from . import test_subscription_invoice_state
from . import test_acl_p9
# refactor: decouple CloudService ↔ Project (red phase)
from . import test_project_decouple
# fix/decuple-project: frontend + controller 收尾 (D1=A + D2=A — TDD red)
from . import test_project_decouple_frontend_endpoint
from . import test_cloud_service_acl
from . import test_task_service_extras
from . import test_chatter_service_injection
from . import test_ux_routing_views
# refactor: CAQA §E HIGH BLOCKER B1 — cross-ws task read regression
from . import test_cross_workspace_task_isolation
# refactor: MCP cloud_service_id → project_id (red phase)
from . import test_mcp_project_decouple_migration
from . import test_mcp_project_lifecycle
from . import test_mcp_acl_isolation
# issue #98 / F-SDS-1: mcp_server / mcp_tool scope-flip write() guard parity (TDD red)
from . import test_mcp_scope_flip_guard
from . import test_mcp_chat_consumption
# issue #48: Project Detail page — project-centric endpoints (TDD red)
from . import test_project_detail_endpoints
from . import test_project_mcp_project_centric
# issue #49 + #51: MCP user-scope URL refactor + unbound project ACL (合 #51 — TDD red)
from . import test_mcp_d5_removal
from . import test_mcp_sidecar_backed_lifecycle
from . import test_mcp_sidecar_hint_endpoint
from . import test_mcp_external_url_creation
from . import test_mcp_acl_unbound_isolation
from . import test_mcp_project_decouple_migration_25
# epic: agent-runtime-pod Phase A (TDD red)
from . import test_agent_pod_phase_a_project_schema
from . import test_agent_pod_phase_a_task_schema
from . import test_agent_pod_phase_a_cloud_service_mn
from . import test_agent_pod_phase_a_new_models
from . import test_agent_pod_phase_a_acl
from . import test_agent_pod_phase_a_migration
# CAQA BLOCKER B-1 + OQ-A-2 (TDD red — impl 待補 16 rule + project/task M:N walk)
from . import test_agent_pod_phase_a_acl_new_models
from . import test_agent_pod_phase_a_project_task_acl_mn
# Reviewer HC-3 + HC-4 (TDD red — impl 待補 @api.constrains + ir.attachment rule)
from . import test_agent_pod_phase_a_attachment_acl
# epic: agent-runtime-pod Phase C (TDD red — paas-operator ① 為 pytest，不在此)
from . import test_agent_pod_phase_c_skill_bundle          # 塊②
from . import test_agent_pod_phase_c_client_webhook        # 塊③
from . import test_agent_pod_phase_c_sse_gateway           # 塊④
# epic: agent-runtime-pod Phase D WS1+WS3 (TDD red — F-PC-1/4/charts 為 host 端，不在此)
from . import test_agent_pod_phase_d_hardening             # F-PC-2/3/5 + F-PA-14
from . import test_agent_pod_phase_d_sse_aclose            # F-PC-6
from . import test_agent_pod_phase_d_enablement            # WS3 UI + seed
from . import test_skill_admin_lifecycle                   # skill backend lifecycle
from . import test_skill_admin_views                       # skill backend views smoke
from . import test_project_form_mcp_skill_tab              # 雙層架構: project form 內嵌 MCP/Skill
from . import test_skill_is_latest_drift_regression        # is_latest drift regression
from . import test_skill_e2e_lifecycle_download            # e2e lifecycle ↔ download
# feat: skill dual-scope (system/user) — schema, ACL, pod loading, migration
from . import test_skill_dual_scope
from . import test_skill_upload_wizard
# feat: skill binding UX — two-layer admin management (2026-06-05)
from . import test_skill_binding_admin
# feat: skill binding UX — portal read-only flag gate (HttpCase, endpoint-level)
from . import test_portal_settings_flag
# feat: platform default agent template (Settings fallback)
from . import test_default_agent_template
# feat: auto-enable agent runtime on project creation (platform toggle + migration)
from . import test_auto_enable_agent_runtime
# fix: platform-default agent template must stay project-unbound (write-time guard)
from . import test_agent_template_default_bind_guard
# feat: F-LLM-3 published+bound template must carry a usable LLM credential (echo guard)
from . import test_agent_template_llm_credential_guard
# refactor: snapshot/resolve 下沉到 agent_session model (#123/#111 共用 refactor)
from . import test_agent_runtime_snapshot_refactor
# feat: #123 cold-start retry — client timeout 分級 + spawn retry + WS connect retry
from . import test_agent_runtime_coldstart_retry
# feat: #111 on-publish push — invalidate_skills + agent_session 推送協調 + skill/mcp 觸發
from . import test_agent_runtime_onpublish_push
# feat: platform Agent runtime status endpoint (Projects-overview Agent card, gitea #126)
from . import test_agent_status_endpoint
from . import test_agent_teardown_unlink           # D2 teardown 孤兒防護（§2.6）
from . import test_reach_sync_triggers              # #279 O5c reach sync 觸發器
# feat: portal task agent chat — model-layer portal access predicates (two-layer-acl-lockstep Layer 2)
from . import test_portal_agent_access
# feat: portal task agent chat — endpoint-level portal access (HttpCase, ORM-vs-endpoint)
from . import test_portal_agent_endpoints
# test: portal task agent chat — SSE gate chain + portal page rendering (HttpCase)
from . import test_portal_agent_page
# fix: portal stream persist — _persist_ai_response must run with system identity (HttpCase regression)
from . import test_portal_agent_persist
# feat: portal task agent chat — minimal task-context hook (TransactionCase)
from .integration import test_ecpay_invoice_real  # noqa: F401 — surface as module to odoo test loader
# feat: agent context enrichment (spec 2026-06-04 — TDD)
from . import test_agent_context_builder
from . import test_agent_context_endpoint
# feat: security access P2 — access control (access_policy submodel + sync + ACL)
from . import test_security_access_access_policy
# feat: security access P2.5 — per-route access (Service-wide → per-route migration)
from . import test_security_access_p2_5_migration
# feat: agent idle reaper (D4) — heartbeat helper + idle reaper cron (TDD)
from . import test_agent_idle_reaper
# feat: agent long-term GC (D3) — reaped 超 T_gc → 完整 teardown → absent (TDD)
from . import test_agent_gc_cron
# fix: agent_session reaped_since invariant — drift/webhook reaped 也補值 → GC 可回收
from . import test_agent_reaped_since_invariant
# feat: Smart Home 半年贈點 Phase 1 — gift_card → eWallet 兌換核心（TDD red）
from . import test_gift_redeem
# feat: Smart Home 半年贈點 Phase 2 — redeem-code endpoint（HttpCase）
from . import test_redeem_code_endpoint
# feat: Smart Home 半年贈點 Phase 3 — admin 手動發點 wizard
from . import test_gift_grant_wizard
# fix: sh_journal_id auto-config idempotent (install+upgrade) + invoice fallback
from . import test_subscription_journal_config
# feat: MCP server cloud service type — hermes pilot seed (template/plan/link)
from . import test_mcp_server_template_seed
# feat: #279 P5 tenant-isolation backfill（baseline NP + agent-ns env re-stamp）
from . import test_p5_backfill
# feat: #271 internal MCP bypass guard — sidecar_backed url in-cluster invariant
from . import test_mcp_sidecar_url_invariant
from . import test_mcp_sidecar_url_invariant_endpoint
from . import test_mcp_sidecar_url_invariant_migration
from . import test_project_detail_deployed_flag
# feat: #275 /woow Project 隱藏開關 — fail-closed flag + session_info 注入 + 卸載清理 lockstep
from . import test_show_projects_flag
# feat: #318 CS 刪除/換 plan 訂閱聯動 — refund 型退點 API + 刪除結算 + 換 plan 退點正名
from . import test_refund_wallet_points
from . import test_service_delete_subscription
# fix: #324 計費雙缺口 — 上游兩支 wallet cron neutralize + 退點 payment gate
from . import test_wallet_cron_neutralized
from . import test_refund_requires_payment
# fix: #327 退點視窗改以 sh_date_of_next_payment 推導（auto-expire 護欄 + 長壽退比例）
from . import test_refund_window_next_payment
# feat: #336 Wave A plan 前台隱藏旗標 storefront_hidden + 三入口過濾
from . import test_plan_visibility_336
# feat: #336 Wave A max_per_partner 每帳號實例上限 + enforcement
from . import test_template_max_per_partner_336
# feat: #336 Wave A HA PaaS 0 元 plan seed + 0 元計費路徑坐實
from . import test_ha_paas_zero_plan_336
# feat: #338 Wave A Odoo Manage MCP 上架骨架 seed（template+plan，is_active=False）
from . import test_mcp_manage_seed_338
# feat: #332 Wave A CS tier→部署規格映射（cs_* 欄位 + seed + runtime overlay）
from . import test_cs_spec_332
# feat: #336 HA PaaS 0 元隱藏訂閱＋開通 API
from . import test_ha_provision_scope_healer
from . import test_hidden_template_chain
from . import test_ha_paas_provision
from . import test_ha_paas_zero_price_billing
# feat: Wave B base reprice（含 SA 四級 B7 拍板）＋新增 n8n t4／SA r1·r3
from . import test_reseed_b_330
# feat: Wave B 208 變體全量斷言（GENERATED by scripts/seed/generate_plan_variants.py）
from . import test_plan_variants_generated
# feat: Wave C PR-C1 後端 variants 回帶（_attach_variants ＋ 三端點擴充，Refs #330）
from . import test_wave_c_variants_attach_330
from . import test_mcp_admin_password
# feat: #357 C05/C19/C20 reset admin password 授權守線與明文外洩面
from . import test_mcp_admin_password_authz
# fix: #366 訂閱方案 product 不應課稅（seed 明寫空 taxes_id + migration force-write）
from . import test_subscription_tax_clear_366
# fix: #367 已訂閱用戶換方案不得重啟 trial（per-partner trial reset guard）
from . import test_trial_reset_guard_367
# feat: #368 PR-1 選擇器重設計後端鋪路（group_key 配對 cycle + specs 顯示）
from . import test_plan_group_keys_368
# feat: #375 三型 MCP admin（k8s/vk/opendesign）上架 seed（template/plan/link/launch contract）
from . import test_mcp_3type_seed_375
# feat: #380 Open Design 完整應用（daemon+console）上架 seed（template/base plan/link/變體）
from . import test_opendesign_seed_380
# feat: HA PaaS 切 woowtech-ha image ＋ chart 0.2.0（chart↔image 原子綁定守線）
from . import test_ha_paas_seed_woowtech_image
# feat: #424① Hermes Agent 完整應用上架 seed ＋ dashboardPublicUrl/apiServerCidrs 注入接線
from . import test_hermes_agent_seed_424
# fix: #449 admin_password 載體策略（config_json / helm_secret）seed 標記與策略傳遞
from . import test_admin_password_carrier_449
# fix: #462（#460 S1）service payload 帶 template.has_admin_gui（前端 Reset gate 唯一入口）
from . import test_service_payload_has_admin_gui_462
# feat: #423 W5a HAOS×KubeVirt 平台側——workload_type 欄位 ＋ stopped state 守線
from . import test_haos_workload_type
# feat: #423 W5a 就緒判定 VM 分支 fail-closed（model ＋ controller 兩層守線）
from . import test_cloud_service_poll_vm_branch
# feat: #423 W5a suspend/resume 映射 runStrategy（防 helm upgrade 喚醒停權 VM）
from . import test_haos_suspend_resume_runstrategy
# fix: #471 is_hidden 拆兩語意——曝光層 vs conceal_instances 實例層的行為矩陣
from . import test_conceal_instances_split_471
# feat: #423 W5b HAOS 上架 seed（template/base plan/link/quota 建模/W2 image pin 閘）
from . import test_haos_seed_423
# fix: #474 check-addon-removal 從 JSON-RPC params 收參——安全檢查的 plumbing ＋ 行為矩陣
from . import test_addon_removal_params_474
# fix: #477 _update_service 模組守衛收斂單一裁決點——success:false 與非 running 皆 fail-closed
from . import test_addon_removal_fail_closed_477
# fix: #484 呼叫端讀齊 operator 具名失敗欄位——dns_failed / pod_retrieval_error fail-closed
from . import test_named_failure_fields_484
# feat: #489 部署／升級 age timeout——轉移狀態超時未就緒轉 error＋診斷（F-423-1 Step 2）
from . import test_cloud_service_deploy_timeout
# fix: #482 停權服務被 redeploy／reconfigure 後留在停權（state 收斂＋workload 收回 0）
from . import test_suspended_redeploy_state_482
# fix: #497 #482 既有卡死資料收斂回停權＋訂閱已恢復者自癒（migration＋cron 縱深）
from . import test_stuck_suspended_converge_497
# fix: #478 marketplace 卡片／configure header 價格標籤（起價摘要，移除硬編 Free）
from . import test_marketplace_price_label_478
# fix: #29 subscription_lifecycle_mixin 內部 subscription traversal 的 cascade sudo
from . import test_subscription_mixin_cascade_sudo_29
# fix: #389 低餘額通知 dedup marker 改跟著投遞下場走（排入失敗／exception 要重送）
from . import test_low_balance_dedup_retry_389
# fix: #520 generic create 吞掉業務例外 → 被拒絕的服務仍留在 DB 並被 cron 部署
from . import test_create_service_validation_rollback_520
# fix: #352 allow 型 Access 政策的 include 不得整批放行（email_domain/everyone）
from . import test_allow_policy_bulk_grant_352
# fix: #525 A1 skip-trial 吞例外 → 錢包已扣／發票已開，畫面卻說失敗（金流殘留＋重試被鎖死）
from . import test_skip_trial_rollback_525
# fix: #522 SA 身分錨點（tunnel_id 等）租戶可改寫 → 宣告式清單 + 不可偽造的 escape hatch
from . import test_security_access_identity_anchor_522
# fix: #525 A2 _create_workspace 吞例外 → 孤兒 workspace（不可見＋永久佔 slug）
from . import test_workspace_create_rollback_525
# fix: #525 A3 _invite_member 吞例外 → 被邀請者已入群、API 卻說失敗（結構缺陷，目前潛伏）
from . import test_invite_member_rollback_525
# feat: #501 全域實例上限硬閘 + 停售開關（config_parameter 載體、售罄現算）
from . import test_global_instance_cap_501
# fix: #518 並發類 DB 例外必須傳到 retrying，不能被 controller 的 except 鏈吃掉
from . import test_concurrency_error_propagation_518
# feat: #530 gateway 交付判準（seed 推導）＋ service payload 的 has_gateway 合約
from . import test_gateway_capability_payload_530
# T2：本檔自建立起未被匯入 ⇒ Odoo test loader 收不到、12 條測試從未執行過
# （2026-08-03 拆成兩張 PR：P-rule 那批帶一條紅燈見 #543，避免污染 develop 全綠基準）
from . import test_ai_assistant
# T2：本檔自建立起未被匯入 ⇒ Odoo test loader 收不到、3 條測試從未執行過
# （#543 已修：原本紅的那條是 fixture 撞 #352 守衛，鷹架 rule 改安全型別後
#  才真的跑到 migrate()，見該檔 _make_legacy_policy 說明）
from . import test_security_access_p_rule_migration
# fix: #535 租戶填入的憑證（type: password）在服務詳情 API 明文回傳 —— 遮罩改 spec-driven
# ＋ read/write 同契約（遮罩後的星號不得被存檔寫回真憑證，D1 實測坐實）
from . import test_credential_mask_spec_driven_535
# fix: #552 chart 版本帳本——四條成功路徑回寫 operator 回報的實際版本
# ＋ reconfigure 的 version fallback（原本恆傳 None ⇒ 全部 15 個 template 都不釘版）
from . import test_chart_version_ledger_552

# fix: #552 層 1 —— seed template 的 helm_chart_version 不得留空
# （留空 ⇒ helm 不帶 version 旗標 ⇒ 抓 registry 當下 latest、帳本記空）
from . import test_seed_chart_version_pin_552

# fix: #552 層 4 —— 沒釘 chart 版本就不准部署（首裝共同咽喉／存量 pending backstop／
# redeploy），＋ poll 補第五條帳本回寫路徑（抓非平台發起的 chart 變動）
from . import test_chart_pin_fail_closed_552
# fix: #553 skip-trial-estimate 回傳例外原文 → 外洩 Odoo 內部 model／欄位技術名
from . import test_skip_trial_estimate_error_leak_553
# fix: #525 B3 reset-admin-password 在提交點之後失敗被回報成「重設失敗」→
# 已生效的一次性明文密碼永久遺失（此處**不得**補 rollback，理由見該檔開頭）
from . import test_reset_admin_password_post_commit_525b3
# fix: #525 B5 輪詢器收屍（`_update_service_status` 404 分支）的 `unlink()` 中途
# 失敗 → 例外逸出 except handler → 被 `_list_services` 的 catch-all 接住，而
# catch-all 自己 log `svc.name` 撞 MissingError 再逸出 ⇒ 今天是整支清單端點 500；
# 那道保護只有一個 token 寬（實測：log 改成不會拋 ⇒ 殘留立刻落地）
from . import test_poll_teardown_rollback_525
# fix: #525 B4 _update_project_mcp_server 吞 ValidationError → 欄位已改、API 說失敗
# （結構缺陷，目前潛伏；含「不包 try 的對照組真的不留殘留」實測）
from . import test_update_project_mcp_residue_525_b4
# fix: #554 MCP sync／connection test 失敗被雙層吞掉 → API 回 success:True（#525 B1／B2 的真修法）
from . import test_mcp_sync_failure_visibility_554
# fix: #559 cloud_service.helm_values 缺欄位級授權 —— 租戶可用 call_kw 直讀明文憑證
# 繞過 #535 的 controller 遮罩層（實測坐實）；補 groups= ＋ 提權入口 ＋ 租戶身分覆蓋
from . import test_helm_values_field_acl_559
# fix: #627 (ii) cron 走的 poll 鏡像 `_poll_and_update_status` 四個 write 分支
# 都沒回寫 helm_chart_version（回寫只存在於 controller 那一份）；本檔驅動 model
# 那一份，並加一支「兩份實作寫入欄位集合必須相同」的結構守衛
from . import test_poll_mirror_release_facts_627
# feat: #627 (i) 對帳 cron —— poll 對 running 不可達，故補一條每日低頻的
# reconciliation（只讀→比對→回寫 release 事實，絕不碰 state）＋ last_release_sync_at
from . import test_reconcile_release_facts_627i
# fix: #556 workspace_access 授權重構 —— ACL unlink=0 + ir.rule create/write
# 自我滿足 + model 無 create()/write()/unlink() 守衛，讓非成員可自助加入/
# 植入第三方，admin 可經 0-owner 視窗自我升 owner（HttpCase，group_user-only）
from . import test_workspace_access_authz_556
# feat: #661 HA 完整版（ha-full）上架 seed —— chart↔image 產品線雙向綁定守線
# ＋ quota lockstep 現算 ＋ base plan/product/M2M（3000/24000）
from . import test_ha_full_seed_661
# fix: #657 project_access 授權補洞 —— identity anchor 缺失 / role 缺省整道
# 守衛被跳過 / 無 owner 列保護，與 #556 同構但刻意不在該 PR 內順手修
# （HttpCase，group_user-only）
from . import test_project_access_authz_657
# fix: #664 workspace_access.create() 的 context-default 繞過 —— role 與
# workspace_id 兩個同型半邊，與 #657 同構（HttpCase，group_user-only）
from . import test_workspace_access_authz_664
# fix: #658 workspace 本體的寫入授權 —— ir.rule 讀寫拆開（收窄到 owner/admin）
# ＋ model 層 identity anchor（owner_id / slug，create 與 write 兩個方向）
# ＋ migration 18.0.1.0.131 退役兩條 sticky 舊 rule（HttpCase，group_user-only）
from . import test_workspace_authz_658
# fix: #677 租戶可經 workspace name 決定自己的 K8s namespace —— slug 由 name 逐字
# 推導 ＋ namespace 是 md5(slug)[:8] 的 32-bit 截斷。改用 stored `ws_hash` 欄位
# ＋ DB 層 UNIQUE（呼叫者身分的 search 在跨租戶下是瞎的）（HttpCase，group_user-only）
from . import test_workspace_ns_hash_677
# fix: #676 cloud_service 的 ir.rule 無 role 條件 ⇒ guest 成員可對該 workspace 的
# 每一筆服務全 CRUD（含走 helm 清理的 unlink）。A 系列＝讀寫拆開後的授權矩陣；
# B 系列＝收窄引入的回歸守線（poll 只被 list/get 兩個無 role gate 的讀取端點呼叫，
# 其寫入改走系統身分）（HttpCase，group_user-only）
from . import test_cloud_service_authz_676

# fix: #698 project/task ↔ cloud_service 關聯層的授權守線。Odoo 的 M2M
# (3)/(4)/(6) 只改 relation 表、零 comodel 權限檢查 ⇒ #676 對 cloud_service 收窄
# 的 ir.rule 管不到關聯層。守衛＝cloud_service_binding_guard mixin（四個入口共用
# 唯一實作），新增與移除兩個方向都套 owner/admin。本檔同時守 #676 的 (0)/(1)/(2)
# 仍由 cloud_service 自己那層擋（HttpCase，group_user-only，全走 call_kw）
from . import test_project_cs_m2m_authz_698
# fix: #705 覆核 findings 的守線。F1＝`default_<field>` context／`ir.default`／
# `copy()` 這族「值不在呼叫者 raw vals 裡」的來源不得繞過 create 守衛；
# F2＝compute 導出的綁定不是呼叫者新增的（空 key 不得炸掉 task create）；
# F3＝反向端候選集必須含移除方向的舊值（判準＝強制否決，不是「有沒有被擋」）；
# F6＝cross-project move 確實會改動綁定集合（行為端已由 #715 反轉：move 現在走守衛，
# 見 test_project_cs_m2m_authz_715_716）（HttpCase，全走 call_kw）
from . import test_project_cs_m2m_authz_705
# fix: #715 ＋ #716 —— #705 post-merge 獨立複驗留下的旁路與三個殘留缺口。
# #715＝task 換 project 讓 `primary_cloud_service_id`（stored compute）靜默換綁，
# 呼叫者不指名任何載體欄位 ⇒ 守衛整道跳過；修法是 write **側專用**觸發器
# （掛成兩側通吃會讓每一次 task create 都退回全比對 ⇒ #705 F2 復發）。
# #716 ①＝批次 create 對 vals_list 取 key 聯集再套回整批 ⇒ F2 換到批次路徑復發；
# ②＝反向端候選集漏掉 archived project（active_test 預設過濾，判準＝強制否決）；
# ③＝`(0,0,vals)` 例外的 docstring 理由為假，B6 把真機制釘住（HttpCase，全走 call_kw）
from . import test_project_cs_m2m_authz_715_716
from . import test_cloud_service_provision_gate_519
from . import test_cloud_service_subscription_binding_519

# fix: #583 transfer_ownership 的授權守衛檢查「被操作那筆記錄」而非呼叫者身分
# （`if self.role != ROLE_OWNER` 的 `self` 是呼叫端指定的 access 列）。RED 是
# TestTransferOwnershipGateIsCallerScoped 那三條：共用同一個 payload、只換身分，
# 修法前三條都回 ValidationError（＝閘門放行、倒在後段商業驗證），修法後只有
# owner 那條還是（陽性對照，不可刪）。可達性已用真 RPC 實證：守衛確實被通過，
# 但完整奪權鏈被 #556 的 _check_owner_row_protected 擋在步驟 4
# （HttpCase，group_user-only，全走 call_kw）
from . import test_workspace_transfer_ownership_authz_583
from . import test_smart_home_authz_524
from . import test_cs_access_control_model_744
from . import test_cs_access_acl_744
from . import test_cs_access_arc_migration_744

# feat: #745 CS AC T3 —— CS 的 operator access sync／CF 命名 env token／錨點
# 分派／reconciler 左集／access endpoints。RED 的三個群集：C 系列（policy 與
# rule 的 sync 觸點寫死 `mapped('route_id')`，對 CS 錨定是空集 ⇒ 完全不打
# operator）、D1（`_push_access_teardown` 是 NotImplementedError seam）、
# E 系列（左集蒐不到 CS 的 access_app_id / cf_policy_id ⇒ L4 判孤兒）。
# H 系列是票面第 6 點的硬驗收：只掛 base.group_user 的租戶走真 HTTP endpoint，
# 斷言 operator 真的被呼叫且 `access_app_id` 真的落地（#739 同構）。
from . import test_cs_access_sync_745
from . import test_cs_access_endpoints_745

# fix: #753 SA／route 的 create 側錨點守衛用 `vals.pop()`，擋不掉
# `default_<field>` context 與 `ir.default` 兩個**預設值載體**（值是在
# `super().create()` 內的 `_add_missing_default_values` 才補回來的，pop 剛好
# 把 anchor 變成「缺席」）。RED 是 A/C 系列；D 系列是 reference_id 陷阱的陰性
# 對照（`required=True` + uuid4 default，無腦壓 False 會炸掉所有合法 create）。
from . import test_sa_anchor_default_carriers_753

# feat: #768 SH AC —— SH route 子模型 ＋ policy 錨點從 exclusive arc（XOR-of-2）
# 擴成「恰好一個」（三腿）。RED 的三個群集：A 系列（第三腿與 SQL CHECK 根本
# 不存在；A6 直接查 `pg_get_constraintdef` 斷言是 `num_nonnulls(...)=1`，因為
# `<>` 串三元是奇偶性——會接受三個都空、拒絕恰好一個）、B 系列（`_check_policy_
# quota` 的二元三元式讓 SH 落 else 分支 ⇒ 拿 SH 的 id 比 SA 的欄位，跨 model id
# 碰撞後的錯誤計數；B2「三個池互不相通」是唯一分得出「數對」與「數全表」的一
# 格）、D 系列（`_anchor()` / `_access_sync_targets()` 漏第三腿的症狀分別是配額
# 靜默失效與「Cloudflare 完全沒被推」，後者與推成功在輸出上不可區分）。
from . import test_sh_access_model_768

# feat: #768 SH AC —— ir.rule 第三腿的授權矩陣（HttpCase，只掛 base.group_user 的
# 帳號、全走 call_kw）。漏改 4 個 domain 任一條的方向是 **fail-closed**：租戶連自己
# 那條 selector rule 都建不出來，而錯誤訊息指向**子表**，沒人會聯想到 parent 換了
# 錨點。F 系列（SA route 錨定與 CS 錨定的回歸對照）不可刪——沒有它，第三腿寫壞了
# 也可能全綠。
from . import test_sh_access_acl_768
# feat: #768 SH AC —— 兩支新端點的授權與白名單（HttpCase，真 HTTP）。J 系列是
# G12 形狀：**arc 是 arity 檢查不是 ownership 檢查**，一條只帶別人 route_id 的
# policy 完全通得過 arc，controller 白名單是唯一擋住它的地方。K1（quota 0 =
# unlimited）沒有 K2 當陽性對照的話，整段 quota 刪掉照樣綠。
from . import test_sh_access_endpoints_768
# feat: #768 SH AC —— operator sync payload／liveness gate／teardown／reconciler
# 左集。B4 的 state 迴圈留了「這個結果不該出現」那一格；D2 用 mock_calls 的索引比
# 大小驗「Access 先於 tunnel」而不是只驗兩者都被呼叫；F 系列漏掉的話每一個**活著
# 的** SH Access app 都會被判成孤兒候選。
from . import test_sh_access_sync_768
# feat: #768 SH AC —— migration 18.0.1.0.150。M2 是本檔與 #744 那支唯一的實質
# 差異：驗的是 `pg_get_constraintdef` 的**定義**而不是 conname，因為本次換的正是
# **同名不同定義**的 constraint，只查名字會讓舊的二元定義原封不動通過。
from . import test_sh_access_arc_migration_768
# fix: #768 驗收缺口（PR #771 退回）—— `smart_home_route` 身分錨點對「預設值載體」
# 的攻防，等價於 `test_sa_anchor_default_carriers_753.py` 之於 SA route。守衛在
# #771 就寫對了（壓伺服器端值而非 `vals.pop()`），但**沒有任何測試證明它擋得住**
# ——那是 #753 那條教訓的更根本版（那次是「守衛看起來有測試、實際上從未被執行」，
# 這次是測試根本沒被寫）。錨點清單從 model 的宣告式常數導出（新錨點自動涵蓋），
# 配 `_ANCHOR_FLOOR` anti-vacuity 下界：清單被清空或打錯字時，本檔每個迴圈都會變
# 成 no-op 而全綠。
#
# ⚠ 可達性與 SA 那邊不同，不要照抄結論：租戶對本 model 的 ACL 是 `create=0`，
# 所以「租戶自己 RPC create 一條 route 並夾帶錨點」打不進來（F1 釘住這件事）；
# **真正可達的是租戶 context 流進平台碼**（`_ensure_primary_route()` 走 `.sudo()`，
# 而 sudo 不洗 context），B1 打的就是那條生產路徑。
from . import test_sh_access_identity_768
from . import test_billing_ssot_guard
# fix: #536 SA 的 sync-control context key 家族可被租戶偽造（#522 的另外三支）。
# `call_kw` 把 client 的 context 原封不動換進 env，所以只判 truthy 的
# `sa_skip_route_sync` / `sa_rule_replace` / `sa_force_access_sync` 都是租戶
# 從 RPC payload 就打得開的逃生門。RED 是每組的第 2 格（A2/B2/C2/D2/E2/F2）。
# 本檔刻意是 HttpCase 而不是 TransactionCase：這條攻擊的前提就是「call_kw 會
# 把 client 的 context 換進 env」，TransactionCase 直接 with_context 只證明
# 「這個 key 有作用」，證明不了租戶構造得出它——而修法（sentinel）之後那種寫法
# 會因為換了值而恆綠，測試退化成沒有鑑別力的形狀。
from . import test_sa_sync_ctx_forgery_536
from . import test_billing_silence_alert

# fix: #528 停權中刪 access policy ⇒ CF 同步被 state gate 靜默略過 ⇒ CF
# reusable policy 永久孤兒。RED 群集：B1／D1（gate 擋掉時 doomed id 沒有任何
# durable 記錄）、B2／B3／C1／D2（之後的 sync 不會把它併進 existing_policy_ids）、
# B5／C2／D3（被 gate 略過是完全靜默的，呼叫端與「同步成功」不可區分）。
from . import test_cf_policy_reap_ledger_528

# fix: #784 刪整條 SA route 時 Access teardown 被無 forced 旁路的 gate 靜默
# 略過，且 policy 的 ondelete='cascade' 是 DB 層 FK CASCADE ⇒ #781 加在
# policy.unlink() 的落帳整支不執行。RED 群集：A1／A2（兩道 continue 各自的
# 可達路徑，operator 根本沒被呼叫）、A4／A5（拆不掉的 id 沒有任何 durable
# 記錄）、B1／B2（兩個相鄰出口）、C1–C5（dead anchor 帳沒有任何回收者）。
from . import test_sa_route_teardown_dead_anchor_784

# fix: #787 A 段第 1 把：`cs_skip_access_sync` 是裸 truthy 閘 ⇒ 租戶用 call_kw
# 的 context 就能讓 `is_protected` 落地而 Cloudflare 完全不知情（fail-open 且
# 無聲）。可達性已實測——租戶自建 workspace 即 owner，ir.rule 給完整寫入。
# RED 群集：A2／B2（兩條可達路徑上偽造的 key 仍抑制了推送）、A3（偽造完全
# 沒有訊號）、G2（in-process 傳 `True` 照樣開閘 ⇒ 判準不是值的同一性）。
from . import test_cs_sync_ctx_forgery_787

# fix: #787 A 段 SH 三把：`sm_skip_access_sync` / `sm_route_cascade_unlink` /
# `sm_force_access_sync` 都是裸 truthy 閘 ⇒ 租戶用 call_kw 的 context 就能
# 打開。可達性已實測（sh-787-reach）：write 與 forced 兩把真的推得動 Cloudflare，
# cascade 那把守衛確實被關掉、只是刪除另有 ACL 擋死（defense-in-depth）。
# RED 群集：A2／B2（偽造仍抑制推送）、C2（例外型別從 UserError 翻成 AccessError
# ＝守衛被繞過）、D2（停權中的 SH 仍推去 CF）、A3／C3／D3（偽造完全沒有訊號）。
from . import test_sh_sync_ctx_forgery_787

# #794：Access teardown 後 DB 回捲 ⇒ 列帶 is_protected=True 復活但 CF 已無 app
# ⇒ UI 顯示已保護、實際免登入即可存取。四處 unlink（SA route / SH route /
# CloudService / SmartHome）同型。RED 群集：A1／B1／C1（拆成功那幾條沒被交還給
# 補償）、A5（pass 1 不 short-circuit ⇒ 整批被拆）、A6（補償誤傷重試錨點）、
# E3（partial 時反方向誤報活著的 app 已死）。
from . import test_access_failopen_compensation_794
# 真交易邊界探針（`-standard`，要 `--test-tags failopen_794` 才會跑）：
# TransactionCase 的 cursor 不可 commit/rollback ⇒ 結構上造不出「整筆交易被
# 丟棄」這個事件，而那正是 #794 的成因本身。
from .integration import test_failopen_real_rollback_794  # noqa: F401
