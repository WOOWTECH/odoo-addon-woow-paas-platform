from . import account_move
# #864 PR-D：所有金流呼叫點共用的 relation-free durable incident ledger。
from . import billing_incident
from . import account_move_invoice_status
from . import ecpay_invoice_cron
from . import ecpg_query_circuit
from . import res_partner_invoice
from . import sh_subscription_fix
from . import sh_subscription_refund_credit_note
from . import sh_subscription_warning_field
# #1599：訂閱回到 in_progress ⇒ 請付款後復權的 cron 立即復權停權中的雲端服務
from . import sh_subscription_resume_trigger
from . import sh_subscription_plan_sa_quota
from . import sh_subscription_plan_sh_mcp
from . import sh_subscription_plan_service_type
from . import sh_subscription_plan_ac
from . import sh_subscription_plan_visibility
from . import sh_subscription_plan_cs_spec
from . import sh_subscription_plan_names_zh_tw
from . import subscription_lifecycle_mixin
# #1766：一條 tunnel 只給一台 HA 接。SH／SA 共用，必須在兩者之前 import。
from . import tunnel_binding_mixin
from . import subscription_lifecycle_cron
from . import low_balance_warning_cron
from . import res_config_settings
from . import workspace
from . import workspace_access
from . import project_access
# #698 綁定守衛 mixin — 必須排在**所有繼承者之前**（cloud_service、
# project_project、project_task），否則 registry 建立時會撞
# "inherits from non-existing model"。
from . import cloud_service_binding_guard
from . import cloud_app_template
from . import cloud_service_secret
# #1257：post-deploy init 的 attempt 記錄（cloud_service 以 Many2one 指向它）。
from . import cloud_service_init_attempt
# fix: #528 待回收 CF Access policy 帳本 —— 三腿（SA route／cloud_service／
# smart_home_route）共用，無相依，位置只要在使用它的 model 之前都可以。
from . import cf_access_policy_reap
from . import smart_home_template
from . import security_access_template
from . import security_access
from . import security_access_access_policy
from . import security_access_access_policy_rule
from . import cloud_service
# feat: #1611 欠費取消／到期後在 /woow 自助續訂（沿用上游續約鏈＋#1599 的復權觸發）
from . import cloud_service_renewal
from . import cloud_odoo_addon
from . import ai_config
from . import project_project
from . import project_task
from . import discuss_channel
from . import oauth_client
from . import oauth_token
from . import oauth_code
from . import oauth_device_code
from . import smart_home
from . import smart_home_provision_operation
# 自動撤權 v1：只能 insert 的撤權帳本（smart_home_route 成功 sync 後在獨立交易寫入）。
from . import smart_home_access_revision
from . import smart_home_route
from . import ai_assistant
from . import mcp_server
from . import mcp_tool
from . import skill
from . import agent_template
from . import agent_session
from . import topup_plan
from . import wallet_transaction
from . import wallet_topup
from . import loyalty_card
from . import payment_transaction
from . import payment_token
from . import res_company
from . import res_partner_wallet
from . import reconcile_report
from . import sh_subscription_plan_reseed_b
# fix: #366 訂閱 product 一律無稅 delegate（migration force-write + 供單元測試直驅）
from . import product_template_subscription_tax
from . import paas_included_vat

# feat: #803 `access_protection_lost` 的週期性收斂者（三腿掃旗標、逐列重推）
from . import access_resync_cron

# feat: #107 啟用 zh_TW（`.po` 自檢 ＋ `_activate_lang`；三個呼叫點共用這一份）
from . import res_lang

# feat: #1159 Q38 post-deploy init 執行面的背景健康監控（mail.thread 單例掛點）
from . import platform_monitor
# #1457：/woow、OAuth 頁面的安全標頭（只套本 addon 路徑）。
from . import ir_http
# #1540：停權／刪除後的限時唯讀資料取回入口（帳本＋session）。
from . import data_retrieval
from . import workspace_invitation
# X-SET-007／X-W-002：login 一律 strip＋lower、不分大小寫唯一、自助註冊驗 email。
# 排在 billing_incident／workspace_invitation 的 res.users 擴充之後 ⇒ MRO 最外層，
# 它們的 write() 看到的已是正規化後的 login。
from . import res_users_login_identity

from . import workspace_quota_inspection
from . import workspace_quota_policy
from . import workspace_quota_audit
from . import workspace_quota_minimum_wizard
# ECPay 站內付 2.0 V4 上線：綁卡 NT$5 驗證授權的取消帳本（DoAction）。
from . import ecpg_bind_auth_void
# LLM-01b：AI 模型分頁的帳戶、金鑰鏡像與設定寫入守衛（DESIGN §2）。
from . import llm_config_guard
from . import llm_account
from . import llm_api_key
from . import llm_event
from . import llm_terms
from . import llm_runtime_state
from . import llm_swr
# 商用驗收 X-SET-007-1／X-SEC-007：自助設定密碼的強度下限、忘記密碼請求排隊（不洩露帳號是否存在）。
from . import res_users_password_policy
from . import password_reset_request
from . import llm_free_usage
from . import smart_home_onboarding
from . import llm_credit_order
from . import llm_credit_void
from . import llm_backoffice
from . import llm_backoffice_operation
from . import llm_backoffice_report

from . import llm_usage

from . import llm_notifications

from . import llm_known_discrepancy
from . import llm_reconcile_local
from . import llm_reconcile_candidate
from . import llm_audit_ingest
from . import llm_account_lifecycle  # noqa: F401 - register Odoo model extensions
