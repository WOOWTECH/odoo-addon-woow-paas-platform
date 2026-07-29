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
