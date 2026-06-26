# Test suite for woow_paas_platform
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
# refactor: snapshot/resolve 下沉到 agent_session model (#123/#111 共用 refactor)
from . import test_agent_runtime_snapshot_refactor
# feat: #123 cold-start retry — client timeout 分級 + spawn retry + WS connect retry
from . import test_agent_runtime_coldstart_retry
# feat: #111 on-publish push — invalidate_skills + agent_session 推送協調 + skill/mcp 觸發
from . import test_agent_runtime_onpublish_push
# feat: platform Agent runtime status endpoint (Projects-overview Agent card, gitea #126)
from . import test_agent_status_endpoint
from . import test_agent_teardown_unlink           # D2 teardown 孤兒防護（§2.6）
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
