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
# HA API tunnel-token 只給 owner／admin（role=user／guest 回 insufficient_role）。
from . import test_ha_api_tunnel_token_role
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
from . import test_auto_recredit_query_gate_864
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
from . import test_portal_paas_workspace_journey
from . import test_portal_paas_bootstrap_20261001
from . import test_portal_paas_capability_20261001
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
from .integration import test_auto_recredit_864  # noqa: F401 — PR-F true-cursor tag
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
from . import test_admin_credentials_tenant_set
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
from . import test_cs_plan_gate_949
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
# fix: #567 Phase A PaaS controller technical exception 原文外洩
from . import test_paas_controller_error_taxonomy_567
# fix: #352 allow 型 Access 政策的 include 不得整批放行（email_domain/everyone）
from . import test_allow_policy_bulk_grant_352
# fix: #521 第 1 層 ac_max_access_seats enforcement（1-A 清單基準，跨訂閱去重）
from . import test_ac_access_seat_enforcement_521
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
# fix: #860 Smart Home delete technical exception 原文外洩
from . import test_smarthome_delete_error_taxonomy_860
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
from . import test_pending_pod_readiness_1156
# feat: #1251 woow-odoo 的 readiness 依 Pod 身分分流（必要組 server＋PG、例行 Job
# 豁免白名單、其他 fail-closed、舊 operator／chart mismatch HARD）——#1156 通用規則
# 對 woow-odoo 的取代；cron 與 controller 兩個入口都測
from . import test_woow_odoo_readiness_1251
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
from . import test_ha_full_apporo_seed
from . import test_pi_agent_seed
from . import test_omnigent_seed
from . import test_code_server_seed
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
# feat: #864 PR-D 帳務 incident 地基（純值 schema／獨立交易／後台人工關單）
from . import test_billing_incident_864
# fix: #864 PR-E `_on_payment_failure` 失敗域拆分（失敗計數 vs 通知/cancel）
from . import test_dunning_failure_domain_split_864
# fix: #864 PR-E CHARGED_NOT_CREDITED 改走獨立交易 incident（含順序／sanitize）
from . import test_charged_not_credited_incident_864
# fix: #864 PR-E double-charge 冷卻閘 config resolver ＋卸載清理登記
from . import test_double_charge_cooldown_config_864

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

# #787 A 段最後一把 `woow_force_issue`：**force 型**（前四把都是 skip 型，安全側
# 相反）。讀取點在 `account_move.py::create_ecpay_invoice`，偽造成功的後果是把一
# 件本來不會發生、而且**不可逆**的對外動作做出來——真的向 ECPay 開一張電子發票。
# 本檔是可達性探針：R 組打一般租戶身分（`base.group_user`），P 組是陽性對照
# （只差 context 裡那一個 key，`upstream.called` 就從 False 翻成 True，證明
# call_kw 真的把呼叫端 context 換進讀取點的 env、也證明觀測點沒壞）。
# 🔴 判定一律走離線替身（patch 上游那支方法本身）⇒ 全程不對 ECPay 送出任何請求。
from . import test_ecpay_force_issue_ctx_forgery_787

# fix: #739 只掛 base.group_user 的租戶經 `/woow` 打 provision 從來沒有成功過
# ——`tunnel_token` 帶 `groups='base.group_system'`，而 `_provision_smarthome`
# 用的是非 sudo 的租戶 recordset ⇒ 那次 write 必 AccessError、記錄卡在
# `state='provisioning'`。既有測試全在 TransactionCase（su env，欄位級
# `groups=` 不生效）⇒ 天生盲目。RED 群集：A1／A2／A3／A4（真租戶 HTTP 打
# provision，紅在 tunnel_token 的 write 上）。B 系列釘「改用平台身分之後原本
# 由租戶身分擋著的檢查一條都沒少」，C1 釘票面第 2 個附帶事實的現況形狀。
from . import test_sh_provision_tenant_rpc_739

# fix: #786 AccessPolicy.write() 在 `super().write()` **之後**才求值
# `_access_sync_targets()` ⇒ 換錨點時只推得到新錨點，舊錨點的 CF Access
# application 繼續引用已經搬走的那顆 reusable policy（授權發散），而那顆 cf id
# 也再進不了舊錨點的 `existing_policy_ids`（operator 唯一的刪除通道）。
# RED 群集：A1／A3／A4／E1（舊錨點根本沒被推）、A1／B2／E1（那顆 id 進不了刪除
# 通道）、A4（推送順序借用 mapped() 就會排錯）、A5（新錨點把舊 app 的 policy
# 當成自己的送去刪）、B1（推不動時那顆 id 沒有任何 durable 記錄）、D1（跨錨點
# 回存錯位而完全無聲）、F1（授權檢查晚於不可回捲的 CF 變更）。
from . import test_access_policy_reanchor_786

# fix: #769 `PUT /api/tunnels/{id}/config` 的 wire 契約破裂（operator 端裸 str ⇒
# FastAPI 綁 query param，client 送 JSON body ⇒ 每次呼叫必 422，被 SH 的
# `except Exception` 吞成 state='error'）。本檔是三腿守線的**腿 2**：攔在 requests
# 真正送出之前，斷言 client 組出來的 PreparedRequest 把欄位放在 body、query 為空。
# 腿 1（真 app 送 body ⇒ 2xx）在 extra/paas-operator/tests/；腿 3（跨層 AST 比對）
# 在 scripts/audit/repo-lint.py 的 tunnel-config-wire-shape——operator 不進 Odoo
# image，跨層那腿在 src/tests/ 裡跑不到。
from . import test_tunnel_config_wire_769

# #794：Access teardown 後 DB 回捲 ⇒ 列帶 is_protected=True 復活但 CF 已無 app
# ⇒ UI 顯示已保護、實際免登入即可存取。**五處**同型（SA route / SH route /
# CloudService / SmartHome / SecurityAccess 本身）。RED 群集：A1／B1／C1／F1
# （拆成功那幾條沒被交還給補償）、A5（pass 1 不 short-circuit ⇒ 整批被拆）、
# A6（補償誤傷重試錨點）、E3（partial 時反方向誤報活著的 app 已死）、
# F3（action_delete 少了自己那一層 ⇒ 補償拿到空清單）、
# G1／G4／G6（落帳撞 OperationalError 時「CF 已拆成功」的事實被丟掉——四腿逐字
# 同型，只鎖 SA 那腿等於把其餘三腿留給下一次重構）。
from . import test_access_failopen_compensation_794
# 真交易邊界探針（`-standard`，要 `--test-tags failopen_794` 才會跑）：
# TransactionCase 的 cursor 不可 commit/rollback ⇒ 結構上造不出「整筆交易被
# 丟棄」這個事件，而那正是 #794 的成因本身。
from .integration import test_failopen_real_rollback_794  # noqa: F401
# #864 PR-D：真 registry cursors／真 40001／rollback 存活與 lock timeout。
from .integration import test_billing_incident_real_rollback_864  # noqa: F401
# #864 PR-E：真 cron tick／獨立 cursor 讀 incident——冷卻閘讀的是「別的連線
# 同輪 commit 的列」，TransactionCase 的 savepoint cursor 造不出這個事件。
from .integration import test_double_charge_cooldown_864  # noqa: F401
# #864 PR-G：callback 預登記／inbox／replay。真跨連線 cursor 是唯一能證明
# 「callback 早於主交易 commit 到達仍接得住」的載體；TransactionCase 的
# fixture 對被測碼的 registry.cursor() 不可見，HttpCase 的 TestCursor 把
# 獨立交易退化成 savepoint ⇒ 兩者都造不出本題的事件。
from .integration import test_callback_replay_864  # noqa: F401
# #864 PR-G：手動儲值 route 的 fail-closed 預登記政策。刻意**不帶**
# `-standard` ⇒ 這是 PR-G 唯一會被 CI（odoo-tests.yml）選中的守線。
from . import test_topup_intent_preregistration_864  # noqa: F401
# #864 PR-G：pay_callback override 的 inbox 落地 ＋ 對外回應 byte-identical。
# 同樣刻意不帶 `-standard`（CI 盲區補償）。
from . import test_ecpg_callback_inbox_864  # noqa: F401

# fix: #785 SH route 的 `_capture_access_teardown()` 用同一個 `None` 表達兩種
# 語意（「沒東西要拆」vs「有東西但預測推不動」），消費端一律 `continue`、不計入
# `failed` ⇒ 呼叫端的 fail-loud（unlink 回滾保錨點）與落 error（_do_cleanup）
# 雙雙判成「這一輪乾淨」，繼續往下刪列 ⇒ CF 上的 app ＋ reusable policy 留著、
# DB 列沒了、無重試錨點也無訊號。修法把那道**預測性** liveness 閘拆掉、判準對齊
# SA／CS 的「手上有沒有 id 要拆」，「推不動」改由真的推一次的成敗決定。
# RED 群集：A2（有 id 但推不動時回 None）、B1（operator 一次都沒被打）、
# B2（沒有 raise、列消失、零訊號）、B3（_do_cleanup 直接寫 deleting ＝假成功）。
# 對照組：A1／A3（訊號的三格判定）、B4（推不動本身不得阻擋刪除）。
# C 系列鎖留言 7951 點名的「清 tunnel_id 前先清 cf_policy_id」跨方法不變式
# （零 gate／零 assert／零測試），守的是 DB 狀態版而非 mock 呼叫順序。
from . import test_sh_teardown_capture_semantics_785

# refactor: #815 SH `_do_cleanup` 缺 SA 那道「事後複查 route 殘留 Access id」的
# 冗餘防線。**非現行 bug**——`_teardown_routes_access()` 裡「push 回 True」與
# 「清 id」綁在同一格、失敗才計入 `failed` ⇒ 「failed 是否為空」與「事後查
# `_has_access_teardown()`」今天等價。補的是 SA 那腿有的冗餘（呼叫端不信 helper
# 的回傳值，回頭看列上的狀態），防的是下一個改該 helper 的人讓「清 id」與「計
# failed」分家（#785 原症狀同型）。
# RED：A1（helper 回 [] 但列還髒 ⇒ 現況寫 deleting ＝假成功）、A5（閘放到
# `_cleanup_tunnel()` 之後就會紅——「閘放錯位置」的唯一機械判別式）、
# A6（兩格的 error_message／log 必須可區分）。
# 對照組：A2（列乾淨仍須收斂，擋住「補了就一律擋刪」）、A3／A4（#813 成敗語意表
# 不得被改）。主測試必須 patch helper：真實路徑造不出「回報乾淨但列還髒」。
from . import test_sh_do_cleanup_lingering_access_815

# fix: #805 (b) 只掛 base.group_user 的租戶經 `/woow` 打 SA provision 從來沒有
# 成功過——`tunnel_token` 帶 `groups='base.group_system'`，而 controller 的
# `action == "provision"` 分支用的是非 sudo 的租戶 recordset ⇒ 那次 write 必
# AccessError、記錄卡在 `state='provisioning'`（重試鈕只認 pending/error ⇒ 鈕
# 消失、無 cron 收斂）。與 #739／PR #797 在 smart_home 上裁定的同一個形狀。
# 既有 SA 測試除一條外全在 TransactionCase（su env，欄位級 `groups=` 不生效），
# 唯一有鑑別力的那條走 `with_user` 直呼 model、不經 controller ⇒ 端點那一格天生
# 沒有覆蓋。RED 群集：A1／A2／A3／A4（真租戶 HTTP 打 provision，紅在 tunnel_token
# 的 write 上）。B 系列釘「改用平台身分之後原本由租戶身分擋著的檢查一條都沒少」
# （三道 gate 全在 sudo 之外——[[two-layer-acl-lockstep]] 要求的 manual access
# check 位置）；C1 釘修法的範圍限定（call_kw 繞過 BFF 仍 fail-closed），D1 釘
# 「遮罩只擋得住第一次 provision」（re-sync 分支不寫 token）。
from . import test_sa_provision_tenant_rpc_805

# #546 A 段：測試收集完備性 gate，以 Odoo 測試本身為載體（runtime introspection）。
# 與 repo-lint 的 `test-collectability`（純 AST、PR-time）是同一條性質的**兩條觀測通道**：
# 這一支是 ground truth，那一支是早期預警鏡像；分歧時去查、不選一邊。
# ⛔ 刻意不共用 repo-lint 的實作——static `test-collectability` 以前置條件鎖定 repo layout
#（root 底下有 src/）；runtime container/addon layout 不符合時現在會 fail-loud 回 wrong-root
# violation，但那只能說明輸入版面不適用，無法回答 Odoo runtime 實際 collectability，故仍需
# 由本檔獨立 introspection（版面差異與 issue #828 見該檔 docstring）。
from . import test_suite_collectability

# fix: #648 (b) CloudService 翻 `running` 早於 Public URL 的 DNS 落地——租戶看到綠燈
# 卻拿 `ERR_NAME_NOT_RESOLVED`。`_is_release_ready` 全文無 DNS 判定，helm deployed ＋
# pods ready 一滿足就寫 running。閘加在 `_is_release_ready`（#423 W5a 的單一事實源），
# 不加在寫 running 的 6 個分支上——那會再造一次 #627 的形狀，且會讓 needs_init 的服務
# 停在 `initializing` 反覆重跑 init 去撞 `init_retries` 上界。
# 🔴 窗口綁 `_transition_anchor()`：#489 的 age timeout 從 `deployed_at`（helm install
# 一回來就寫）起算，閘若自己另計時，「慢部署 ＋ DNS 未落地」會把 age 推過門檻寫成
# error，而 error 沒有任何 poll 分支救得回來。
# RED：判定層（窗口內 missing 不得就緒、FQDN 結尾點）、兩層 poll 各一（只改一層會被
# 前台每 5 秒那條覆寫回去）、窗口過期放行並留旗標、旗標清得掉。
# 🔴 不變式：`test_gate_can_never_cause_age_timeout` ＋ clamp 兩條合起來證明閘擋住時
# age 必定小於 timeout。對照組：DNS 解析得到／無 subdomain／resolver unknown／kill
# switch 四條釘住「不得修過頭」。
# 另有一條補 repo-lint 盲區：`check_config_parameter_uninstall` 只掃 `config_parameter=`
# 字面 kwarg，本題兩個 key 走模組常數 ＋ get_param（同 #489 先例）⇒ 忘了登記進
# hooks.py 不會紅，用測試補上。
from . import test_public_url_dns_gate_648

# fix: #648 (a) operator 用**具名欄位**回報 expose 失敗（`route.status` /
# `route.dns_failed`），平台 `_execute_deploy` 一次都沒讀過 ⇒ DNS record 建不成時
# 租戶拿到永遠不會解析的 hostname 而服務照樣往 running 走。#484 為 SmartHome 補過
# 同一條縫，CS 這半漏接。
# 🔴 本檔最重要的是 A5 不是 A1：判準必須是「明說的失敗」而不是「東西不在」。
# operator 的 `cloudflare_enabled` **預設 false**，且舊 operator 沒有這兩個欄位 ⇒
# 「沒回 route 就當失敗」會讓沒開 CF 的環境**每次部署都翻 error**，比原 bug 嚴重。
# RED：A1（dns_failed 不得留在 deploying）、A2（訊息指名 hostname ＋ 給出路）、
# A3（status=failed 亦然）、A6（判 error 也不得丟掉帳本）。
# 守門對照組：A5（缺席不得 fail-closed）、A4（舊 operator 缺鍵不得算失敗）、
# 健康 route 不觸發閘——這三條紅了代表守線被改成用缺席當判準。
from . import test_route_dns_failed_648

# fix: #1071 route 的真錨點 `security_access_id` 在 write 側沒有歸屬守衛——
# 對稱 #848（policy）／#941（rule）補 `_assert_route_anchor_move_authorised`。
# 🔴 判別式＝ B2c（自 `test/1054-reach` 移來，run 2674 job 8018 唯一那條紅，
# 形狀 `AssertionError: Exception not raised`）：受害者 SA **無訂閱**時
# `_check_route_quota` 提前 continue ⇒ 今天擋住有訂閱那格的 read 副作用不發生，
# 那條路上零守線。B2 一併移來並收緊成「必須紅在新守衛上」（兩邊都是
# `AccessError`，class 零鑑別力 ⇒ 判準是 traceback 的函式名）。
# 注入對照：C4（patch 掉守衛，攻擊必須翻回落地）；回歸面：C1／C2／L1。
from . import test_route_anchor_move_guard_1071

# fix: #822 `action_provision` 第一次 provision 的「孤兒窗口」——`create_tunnel` 已
# 在 CF 建出 tunnel、而它的 id 還沒落進 DB 的那一段——四支 except 一律零補償
# ⇒ 守衛 `if self.tunnel_id:` 不成立、DB 沒有任何東西指向剛建的那顆。
# 🔴 本檔的判準是「參照 or 回收」的孤兒不變式，刻意不預選修法（#822 §8 三條方向
# 在這個判準下都算通過），比對的是**每一次 create_tunnel 的回傳 id**——固定
# return_value 的 mock 在這裡會假綠（重試那顆與第一顆同 id）。
# RED：A2/A3（AccessError 形狀，真機制）、B2/B3（identity write 炸非 AccessError，
# 注入）、D1/D2（create_tunnel 自己炸＝不可判定格，留可對帳記錄）、E1（補償自己
# 失敗）、A4（補償成功後要回得去可重試狀態，⚠ 設計取捨）。
# 陽性對照（修復前後都必須綠）：C1 守衛生效 0 次、C2 happy path 不得誤刪、
# C3/C5 窗口**外**失敗不得補償刪除、C4 窗口外不得掛孤兒標記（標記不得變雜訊）。
from . import test_sa_provision_orphan_tunnel_822

# fix: #798 `security_access_access_policy.cf_policy_id` 沒有**任何**欄位級寫入
# 守衛（欄位無 `groups=`、model 無 create strip／write reject、ACL 對
# base.group_user 是 1,1,1,1、ir.rule 只過濾列不過濾欄位）⇒ 租戶對自己
# workspace 內的 policy 列寫得進任意字串。而這個欄位餵的是 operator **唯一的
# 刪除通道** `existing_policy_ids`，也是 `cf_access_policy_reap._trusted_ids_for()`
# 判「可信」的那一半——那道判準假設「列上的值是伺服器擁有的」，本 issue 正是
# 那個假設不成立。
# 🔴 A 系列補的是 #798 自陳缺的那一格：該 issue 明寫「沒有跑過真的租戶 RPC ……
# 端到端未驗」。用 `with_user` + `assertRaises` 代替不行——`TransactionCase` 是
# su env、也模擬不到 `call_kw` 把呼叫端 context 整包換進 env 的那條載體。
# RED：A1（覆寫既有 cf id）、A2（從空種進去＝跨租戶刪除的形狀）、B1/B2/B3
# （create 側三個載體：vals／`default_<field>` context／`ir.default`）、E1/E2
# （sudo 不是開閘）。
# 陽性對照：每一格攻擊前先跑一次同一條 RPC 的合法欄位寫入／注入同機制的
# `decision`——少了它，「守住了」與「這個通道在本環境根本不作用」不可區分。
# 陰性對照：B1 另外斷言 create **本身**成功且其他欄位正常（「create 直接炸掉」
# 對「攻擊者的值沒落地」同樣是綠的，同型翻車見 `test_sa_anchor_default_carriers_753`）。
from . import test_policy_cf_id_write_guard_798

# fix: #732 owner 對自己那列 write({'role':'admin'}) ⇒ workspace 永久 0 owner。
# 三道既有守衛全漏（owner-row 守衛只擋非 owner 呼叫者、last-owner 不變式只掛
# unlink、owner_count constraint 只在新值是 owner 時觸發）。新守衛＝write 路徑的
# last-owner 保護：demote 會讓該 workspace 剩 0 owner 時 ValidationError。
# 🔴 判準比對記錄不比對呼叫者（數 demote 之外剩幾個 owner，不是 blanket-deny
# owner 列）——雙 owner SQL 直造的陽性對照釘住這個形狀；su/group_system 豁免
# 由「promote-first 已被 owner_count constraint 擋死」逼出（詳見該檔檔頭），
# 平台側 demote-then-promote 整段轉移有專屬陽性對照。
from . import test_workspace_access_last_owner_732

# fix: #533 controller 用裸 `except Exception` 收掉並發類 DB 例外
# （`SerializationFailure` 40001 是 `Exception` 子類）並正常 return ⇒ 例外從未
# 逸出到 `retrying()` 的攔截點（它在 controller 的 except 鏈**外層**）⇒ 本來
# 會被自動重試而成功的操作變成使用者看到的失敗。
# 三站各自有自己的注入斷言、不共用一格綠燈：`api_service_skip_trial`（issue
# 本體）、`api_service_reset_admin_password`（枚舉補到——它的 model 層有兩處
# `except OperationalError: raise` 被 controller 吞回去，那兩處今天等於沒作用）、
# `api_service_skip_trial_estimate`（枚舉補到，side-effect-free 但缺口相同）。
# 🔴 一律走真 HTTP：每條斷言的結論都取決於「例外**逸出被測碼之後**會發生什麼」，
# 而 #572 已實測記載直呼與真 HTTP 在這種形狀下語意分歧、直呼會失去鑑別力。
# 🔴 注入的 40001 必須用 `__setstate__` 補上真 pgcode（Odoo 自己在
# `test_http/controllers.py::upload_file_retry` 的手法）——直接建構的 pgcode 是
# None，`retrying()` 的 `errorcodes.lookup()` 會先 KeyError，重試路徑根本走不到。
# `test_the_injected_exception_carries_a_real_pgcode` 把這個前提釘成斷言。
# 陽性對照：三站各有一條「不注入時端點成功」＋一條「注入真的被呼叫到」。
from . import test_concurrency_retry_533

# fix: #740 unlink() 的外部副作用送在授權檢查之前 —— 授權前置檢查（authorization
# preflight）。租戶刪除必失敗（ACL unlink=0），但 Cloudflare 那半在被拒之前就已經
# 送出去且回捲不掉。
# 🔴 本檔的強斷言是「授權被拒時外部呼叫數為零」＋逐一列名「哪些 method 沒被呼叫」；
# `test_smart_home_authz_524.py` 的 C5 守的是 #524（不得送出**別的租戶**的 id），
# 對空清單恆真 ⇒ 修好之後它照樣綠但綠的理由變了，**不是**本題的守線，維持原樣。
from . import test_sh_unlink_authz_preflight_740

# fix: #848 access policy 的三個錨點 Many2one 在 model 層沒有歸屬守線。缺口是
# **write 側專屬**的——框架 `_create` 尾端會對新建出來的列再跑一次
# `check_access('create')`，`write()` 只在開頭對寫入前狀態檢查一次、寫入後零複驗。
# 可利用性落在「兩個 workspace 的共同成員」而非陌生人：三條 member-read rule 的
# domain 都沒有 role 條件 ⇒ 讀得到目標錨點就搬得過去，兩道既有檢查全過。
# ⛔ 本檔的守衛擋不到 #849——現行機制見留言 9445：寫入直接落在 policy 自己的
# stored column 上，route_id 不動（曾誤判為 stored related 帶 inverse 委派、
# 寫到來源列，該說法已於留言 9433 撤回）。
from . import test_policy_anchor_ownership_guard_848

# feat: #803 `access_protection_lost` 打開之後沒有任何週期性收斂會把它推回去。
# `_access_mark_protection_lost` 的 docstring 逐字寫著「平台**沒有**週期性 Access
# re-sync，只有 provision / resume / 直接寫 is_protected／login_*／session_duration
# 三種事件會推」——三種都要租戶或維運自己動手 ⇒ 沒人動，對外 hostname 就一直
# 可達且未受保護。本檔守那條缺席的收斂路徑（三張表：SA route／SH route／CS）。
# 🔴 形狀守線在 A5/B5/C5/D1：`search(domain, limit=N)` 會被永久推不動的旗標列
# 自己堵死，而那個狀態與「今天沒事做」在輸出上完全一樣（#791 F1 同型）。
# D4 釘住「零命中那輪也要出聲、且那一行自帶三個計數」——空手與全被擋是兩件事。
# D5 釘住 cron 的回存過得了 #854 的 `access_app_id` 欄位級守衛。
from . import test_access_resync_cron_803

# feat: #36 試用中變更方案**一定**結束試用並開首期帳單——SA / SH 兩 surface。
# `ChangePlanModal` 的警告文案逐字保證三件事（結束試用／開始計費／首期帳單），
# 但那個「一定」是**後端**保證的：change-plan 的 endpoint gate 與
# `_create_and_confirm_subscription` 的 `entity_has_prior_sub` 讀同一個
# `subscription_id` 欄位、中間沒人清它 ⇒ 該旗標結構性恆 True ⇒ 目標方案的
# `sh_trial` 完全不影響結果。⇒ **trial→trial 也會當場扣款**，照 issue #34 Q9
# 字面判成「靜默」恰好會讓真正扣錢的那條路徑不出聲（Eugene 已改判為 P2）。
# CS 已有等價守線（test_trial_reset_guard_367），本檔補盤點出缺的兩格：SA 整個
# 沒有、SH 只有「非 trial」那半而缺「首期帳單」那半（該檔刻意不驅動 invoice）。
# 🔴 每個 surface 配一條方向相反的陽性對照（全新首購＝有 trial ＋ 零帳單），
# 否則 `assertTrue(invoices)` 可能只是反映「任何新訂閱都開發票」而恆真。
# 🔴 不共用 mixin 放 `test_*`（loader 走 `__dict__`，mixin 上的不會被執行）；
# 共用的只有 module-level helper function。
from . import test_change_plan_ends_trial_36

# #750 第 2 項：SA 卡片規格 sa_max_access_policies 的 0=unlimited 顯示。
# 🔴 routes 側（0=none allowed，語意相反）的顯示繫於 #770，本檔把「維持印
# 字面 0」寫成守線——防「一個特判同時蓋到兩個欄位」。
from . import test_sa_policies_quota_display_750

# #833：軟刪除的 tunnel 必須與「還在、只是沒連上」可區分（判別式 ＋ 陽性對照 ＋
# 舊 operator 缺鍵對照）。⚠ 少了這一行，整檔會**從未執行**而測試套件照樣全綠。
from . import test_tunnel_soft_delete_833

# #833 順帶：teardown 成功後 `tunnel_status` 被凍結在 connected（平台自己刪的
# tunnel，畫面卻還是綠燈；`action_refresh_status` 開頭的 tunnel_id 早退讓它再也
# 更新不到）。
from . import test_tunnel_status_after_teardown_833

# #107：zh_TW 啟用鏈（判別式先造 inactive 世界；`.po` 自檢的三種壞法；`<function>`
# 沒被包進 noupdate=1 的結構守線）。⚠ 少了這一行，整檔會**從未執行**而測試套件
# 照樣全綠。
from . import test_lang_zh_tw_activation_107

# #107 第四層（locale formatter）：日期／時間／數字格式必須跟隨 Odoo 使用者語系
# 的靜態守線。⚠ 少了這一行，整檔會**從未執行**而測試套件照樣全綠。
from . import test_locale_formatting_guard_107

# 量測: #849 RED-A —— 寫 policy 的兩個 stored related 投影欄位
# （`security_access_id` / `smart_home_id`）會怎樣。⛔ 本檔是**量測**不是守線。
# #849 原文說它們帶 inverse 委派、寫它們會寫到來源列；實測要驗的是那個機制
# （related 的 readonly 預設是 True ⇒ `setup_related` 不裝 inverse），以及
# 真正的形狀：`store=True` 讓值直接落在投影欄位那根 column 上，而那根正是
# policy 的 ir.rule 三條腿裡兩條的歸屬判準來源 ⇒ **歸屬分裂**。
# 🔴 判準刻意不是「通過／不通過」兩格——靜默丟棄／落地／落地後被 recompute 蓋回
# 三種輸出不同但都不報錯。DB 值量兩次、ir.rule 歸屬量 2 身分 × 2 時點。
from . import test_policy_related_anchor_projection_849

# #849 GREEN：五個投影欄位（policy 2 ＋ rule 3）的直寫守衛。兩半缺一不可——
# G 系列量「租戶／superuser 直寫被大聲拒絕且不落地」，L 系列量「框架自己的
# related 重算**不被誤擋**」（守衛擋的是 key 出現在 vals 裡，而重算天天在改
# 這幾根 column；誤擋的症狀是每次搬 route／換錨點都炸，但要在測試裡看到它不炸
# 才算數）。C 系列蓋 create 側三個載體，只主張不變量不主張機制。
from . import test_policy_projection_guard_849
from . import test_change_plan_seat_gate_773

# fix: #941 rule 子模型的真錨點 `policy_id` 沒有歸屬守線（write 側），與 #848 在
# policy 側修掉的是同一個框架缺口：`BaseModel.write` 只在**寫入前**驗一次
# ir.rule、落地後零複驗 ⇒ 「把既有 rule 搬進他人 policy」框架不擋，而 #929 的
# 投影欄守衛只逐 key 比對三個投影欄、`policy_id` 不在其中。
# A 系列走真 RPC 量攻擊面（含 x2many 側門與「受害者錨點不得被 sync」），
# B 系列把 create 側的框架 backstop 釘成契約，C 系列證守衛沒擋掉合法路徑
# （同 workspace 搬移／su／pull 方向／cascade），L 系列證 controller 與框架
# 自己的 related 重算沒被誤擋。
from . import test_rule_reparent_guard_941
# fix: #864 格1（PR-A）api_invoice_pay 半成交 —— `_pay_from_wallet` 分段寫入，
# 例外被 except 鏈吃掉後框架照常 commit ⇒ 發票 posted（序號已燒）＋點數已扣，
# 但沒有 payment、沒有 reconcile，而畫面說失敗；再按一次會被**扣第二次**。
# 三種注入深度（journal 缺失／payment 建好後／reconcile 之後）各配一條深度斷言，
# 併發那組證 40001 傳得到 retrying()；前端失敗分支重抓錢包走靜態掃描。
from . import test_invoice_pay_rollback_864
from . import test_invoice_pay_flagless_guard_980
from . import test_invoice_issue_policy_864

# #990：老化告警 cron 的 batch 化。unbounded 掃描＋逐 move 獨立 cursor 的 N+1，
# 而 naive `limit` 會讓後排永遠餓死（逾期條件不自消 ⇒ 舊批佔死視窗，#791 F1 的
# 同一個形狀）。核心判準是「視窗會前進」，觀測用 incident 存在性而不是 marker
# （marker 是修法的實作細節，拿它當判準會讓 RED 紅在錯的地方）。
from . import test_stale_manual_aging_batch_990

# #993：`woow.billing.incident.trade_no` 統一存 provider TradeNo（Eugene 裁決 A）。
# 病＝欄位沒有語意宣告，PR-E 寫我方 reference、PR-G 寫 ECPay TradeNo，而 upsert 的
# explicit_evidence 讓後者被前者無聲覆寫——那是平台唯一一份 ECPay 交易編號。
# T1/T3 刻意走生產碼的 `_log_charged_not_credited_incident`，⛔ 不自己組 snapshot
# （測試自己傳 trade_no 的話，改生產碼不會讓它轉綠＝零鑑別力）。
from . import test_trade_no_semantics_993

# fix: #896 `_check_policy_quota` 的「數池子」`search_count` 沒有 `sudo()` ⇒
# `ir.rule` 濾掉呼叫者看不到的列 ⇒ 少算 ⇒ 配額靜默放行（症狀與「方案上限很大」
# 輸出完全相同，沒有訊號分得出來）。少算是 **0-或-全**：ir.rule 判歸屬走的就是
# `_ANCHOR_POOL_FIELD` 那幾根 column、與池的 domain 同一根 ⇒ 非成員看到 0、成員
# 看到全部。#924 已在真沙盒量到落差（非成員 0 vs sudo 2），端到端不可達靠的是
# stored related 的 compute_sudo 重算與 #848 的 AccessError 前置兩個**外部**性質。
# 🔴 測試刻意**直接以寫入者 env 呼叫守衛**：走 compute_sudo 重算那條修前也會擋
# （測試會「修前綠」＝零鑑別力），走 CS→CS write 那條修前紅在 #848 而非本格。
from . import test_policy_quota_pool_sudo_896
# #1052：8 支 _woow_migrate_* delegate 只有 1 支有刪除守衛；本檔掃 migrations/ 的呼叫點
# 逐一 hasattr（陽性對照＝_KNOWN 的 13 對必須都被掃到，含 5 支不帶前綴的）。operator_contract.py 是共用件、
# 刻意不在此登記（同 smart_home_fixture.py）。
from . import test_migration_delegate_liveness

# #1052：SmartHome／SecurityAccess 的 _do_resume guard 改問 mixin 的 _is_halted() seam
# （cron 已經在用它）。RED-first：兩條方向相反的斷言在 develop 上都紅——見檔頭 RED 預測。
from . import test_lifecycle_is_halted_seam_1052

# #1054（階段 1 量測，**未修法**）：配額計數的第二／第三家族走 `len(o2m)`，而
# `One2many.read` → `search_fetch` 的 docstring 逐字說會套 access rules ⇒ 與 #896
# 同機制、不同承載體。🔴 第一趟量測（run 2668）翻掉了「非成員 = 少算載體」這個
# 前提：o2m 取值前先有一道對**父記錄**的 `check_access('read')`
# （`models.py:4124 fetch`）⇒ 讀不到錨點的人 fail-closed（AccessError），讀得到的
# 人必是成員、而成員看得到整池（0-或-全）。所以本檔的判別式改成直接量「有沒有
# 『寫得動父記錄、卻只看得到一部分子列』的身分」（A6／C5），三種結論由紅綠讀出。
from . import test_quota_len_o2m_reach_1054

from . import test_helm_values_addon_builder_1052  # #1052：addon helm values builder characterization

# #1196：租戶自助取 tunnel token 的閘門。端點 sudo() 跨過欄位的 group 限制，
# 所以它的三道閘門就是全部的安全性，每一階都要有紅綠。
from . import test_sa_tunnel_token_portal_1196

# #1196：route 改名。守的是「值變了 ⇒ CF 也跟著搬」，尤其 Access app 由 hostname
# 定義——改名不重推會讓新網址變成免登入入口而 UI 仍顯示已保護。
from . import test_sa_route_rename_1196

# #1234：route 健康探測。分類樣本取自正式站實測；端點守「探測目標只來自 DB」。
from . import test_sa_route_health_1234

# #1234 第二步：route 流量。各種拿不到的原因（缺 CF analytics 權限、operator 太舊）
# 都要是可解釋的值，不是錯誤。
from . import test_sa_route_traffic_1234

# SA 詳情頁 tunnel 狀態改用即時值（只讀疊加）。守「成員看得到即時值、這條路徑不寫 DB、
# operator 拿不到時沿用 DB 值」。
from . import test_sa_live_tunnel_status

# SH 詳情頁 tunnel 狀態改用即時值（SA #1256 的同構修正）＋ HA status 端點改只讀。守
# 「parser 與舊 refresh 判讀一致、成員看得到即時值、這條路徑不寫 DB、operator 拿不到
# 時沿用 DB 值、HA status 不再 action_refresh_status 且回應 key 不變」。
from . import test_sh_live_tunnel_status

# Smart Home 開通重試：有 tunnel 的 error／pending 在同一條 tunnel 上收斂（不再呼叫
# create_tunnel）；active／suspended 照舊拒絕；收斂失敗維持 error 並保留 tunnel。
from . import test_sh_provision_retry

# #794 前端接上：`access_protection_lost` 必須出現在三腿畫面實際讀的詳情 payload
# （SA sa.to_dict routes／SH include_details routes／CS _access_to_dict）。key 缺席時
# AccessAppCard 讀到 undefined，fail-open 警示會靜默消失。
from . import test_access_protection_lost_ui_payload

# #1261：CS／SH 換到 0 席方案時，仍受 Access 保護就擋（0 席看不到 Access 頁籤，換過去
# 保護關不掉、對外 403）。守「擋在退點／關舊訂閱之前、關保護後放行、卡死記錄在 0 席下
# 仍關得掉、SA 刻意不適用」。
from . import test_change_plan_protection_gate_1261

# #1261 SA 換方案的 route 數降級 gate（#773 席位 gate 的同構）：3 條 route 從 3 組降到
# 1 組必須在退點／關舊訂閱之前被擋、⛔ 不刪任何 route；0 組＝不准（不是 unlimited）；
# route 數 ≤ 目標 cap 的降級與升級照常放行；AST 釘 gate 排在第一個寫入之前。
from . import test_sa_change_plan_route_gate_1261

# #1261：portal（share）帳號不能用 workspace。守「邀請 portal 被拒且不留列、同 email
# 有 internal 時落到 internal、portal 打 workspace 端點拿結構化錯誤而非 AccessError
# （含殘留舊成員列）、internal 行為不變」。
from . import test_portal_user_workspace_1261

# #1261 (d)：skip-trial 後 sh_trial_end_date＝昨天是「試用最後一天（含）、start＝
# trial_end + 1」的日期慣例，不是 off-by-one；釘住它，免得被「順手修正」成今天。
from . import test_skip_trial_dates_1261

# #1257：post-deploy init 的執行管控（欠 init、attempt、lease、CAS、失敗分類、
# migration），含 #1266（suspend 吃掉 init）與 #1267（reconfigure／rollback 繞過
# init）的重現。會走到 init 入口的測試用真 cursor＋真 commit＋事後清理。
from . import test_init_execution_control_1257

# #1294：非系統管理員的工作區 owner/admin 刪不掉自己的 SH（ACL unlink=0 ＋ 前置檢查
# 連 route 子表一起驗）。守 owner/admin 經真 HTTP 刪得掉、user/guest／跨工作區仍被拒、
# Cloudflare 清理失敗時記錄保住、route 仍沒有單獨刪除入口。
from . import test_sh_tenant_delete_1294

# #1295：工作區底下還有未刪除的 SA／SH／cloud_service 時拒絕封存（租戶的「刪除工作區」）；
# 守衛在 model 層 write()，走真 HTTP 驗產品端點與 ORM 直呼兩條路徑。
from . import test_workspace_archive_guard_1295

# 租戶經 `/web/dataset/call_kw` 直打 SA／SH／工作區 model 的 ORM 層守衛：SA 建立閘
# （無訂閱裸建＝免費 tunnel）、SA／SH `subscription_id` 不得清空／改指、停權中不得
# 被租戶帶回服務狀態（含 SA re-sync）、SH 直打 provision 不得先開 tunnel 再炸、工作區
# 封存／還原 owner-only（admin 擋）。一律 HttpCase＋真租戶 session，每格配陽性對照。
from . import test_tenant_orm_lifecycle_guards
