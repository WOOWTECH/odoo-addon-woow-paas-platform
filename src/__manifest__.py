{
    "name": "Woow PaaS Platform",
    "version": "18.0.1.0.259",
    "category": "WOOW",
    "summary": "Woow PaaS Platform - Base Module",
    "description": """
        Woow PaaS Platform 的基礎模組。
        提供 PaaS 平台的核心功能。
    """,
    "author": "Woow",
    "website": "",
    "depends": [
        "base", "web", "project", "mail", "bus",
        # X-SET-007：覆寫 /web/signup 必填檢查與 res.users 註冊／登入身分正規化
        # （src/controllers/signup.py、src/models/res_users_login_identity.py）。
        # 原本就經 portal 傳遞安裝，這裡明列以固定 MRO／controller 繼承順序。
        "auth_signup",
        # D19 extends the final native invoice/payment controllers.
        "account_payment",
        "ai_base_gt", "odoo_ai_assistant_chatgpt_connector",
        "sh_subscription",
        "sh_subscription_wallet",
        "payment_ecpay_ecpg",
        "ecpay_invoice_tw",
    ],
    "data": [
        "security/ir.model.access.csv",
        "security/ir_rules.xml",
        "data/cloud_app_templates.xml",
        "data/cloud_app_documentation.xml",
        "data/smart_home_templates.xml",
        "data/security_access_templates.xml",
        "data/topup_plans.xml",
        "data/subscription_products.xml",
        "data/subscription_template_links.xml",
        # Wave B：208 變體（GENERATED；必排在 base links 之後——M2M noupdate 鐵律）
        "data/subscription_products_variants.xml",
        "data/subscription_template_links_variants.xml",
        "data/subscription_journal_config.xml",
        "data/paas_included_vat_sync.xml",
        # #778：SSOT 對撞防護的 -u 路徑（<function> 每次升級重跑；
        # fresh-install 路徑在 hooks.post_init_hook）
        "data/billing_ssot_guard.xml",
        # LLM：內部額度轉換商品／sequence 先於背景 cron（DESIGN §1.2）。
        "data/llm_credit_data.xml",
        "data/llm_cron.xml",
        "data/llm_notifications_cron.xml",
        "data/llm_backoffice_cron.xml",
        "data/llm_reconcile_cron.xml",
        "views/menu.xml",
        "views/workspace_views.xml",
        "views/workspace_quota_inspection_views.xml",
        "views/billing_incident_views.xml",
        "views/llm_terms_views.xml",
        "views/llm_runtime_state_views.xml",
        "views/llm_backoffice_views.xml",
        "views/llm_backoffice_wizard_views.xml",
        "views/llm_known_discrepancy_views.xml",
        "data/ai_assistant_data.xml",
        "data/mcp_server_cron.xml",
        "data/cloud_service_cron.xml",
        "data/smart_home_provision_prototype_cron.xml",
        # #1159 Q38：背景健康監控的 mail.activity 掛點（mail.thread 單例）
        "data/platform_monitor_data.xml",
        # #778 配套 3：計費靜默死亡監控 cron
        "data/billing_silence_cron.xml",
        "views/project_task_views.xml",
        "views/project_views.xml",
        "views/ai_config_views.xml",
        "views/cloud_app_template_views.xml",
        "views/smart_home_template_views.xml",
        "views/security_access_template_views.xml",
        "views/security_access_views.xml",
        "views/cloud_service_views.xml",
        "views/cloud_odoo_addon_views.xml",
        "views/mcp_server_views.xml",
        "views/skill_views.xml",
        "views/agent_template_views.xml",
        "views/ai_assistant_views.xml",
        "views/topup_plan_views.xml",
        "views/gift_grant_wizard_views.xml",
        "views/res_config_settings_views.xml",
        "data/subscription_lifecycle_cron.xml",
        "data/mail_templates.xml",
        "data/llm_notification_mail_templates.xml",
        "data/billing_mail_lang_sync.xml",
        "data/workspace_invitation_mail.xml",
        "data/low_balance_warning_cron.xml",
        "data/cron_topup.xml",
        "data/ecpay_invoice_cron.xml",
        # #1680 Workspace quota 背景重試／對帳
        "data/workspace_quota_cron.xml",
        "data/agent_session_cron.xml",
        # #1540：限時唯讀資料取回入口的到期關閉 cron
        "data/data_retrieval_cron.xml",
        # 商用驗收 X-SEC-007：忘記密碼請求的非同步處理 cron
        "data/password_reset_request_cron.xml",
        "data/reach_reconciler_cron.xml",
        "data/reconcile_report_cron.xml",
        # #784：待回收 CF Access 物件帳本的 dead-anchor 回收 cron
        "data/cf_access_reap_cron.xml",
        # #803：access_protection_lost 的週期性收斂 cron（頻率推導寫在該檔註解）
        "data/access_resync_cron.xml",
        "data/agent_template_demo.xml",
        "data/agent_template_default.xml",
        "data/demo_system_skill.xml",
        "views/account_move_views.xml",
        # #1610：訂閱上的「允許復權（已人工確認付款）」（帳務管理員）
        "views/sh_subscription_resume_views.xml",
        "views/paas_app.xml",
        "views/portal_templates.xml",
        "views/oauth2_templates.xml",
        "views/connect_login.xml",
        "data/smart_home_onboarding.xml",
        "views/oauth_client_views.xml",
        "data/oauth_clients.xml",
        "data/oauth_scope_updates.xml",
        "data/oauth_ha_provision_scope_update.xml",
        "data/oauth_redirect_mode_update.xml",
        "data/oauth_device_code_cron.xml",
        "data/oauth_device_grant_update.xml",
        # #107：啟用 zh_TW 的 install / -u 雙路徑（⛔ 該檔不可包 noupdate=1，
        # 否則 -u 路徑靜默不跑；啟用前的 .po 自檢在 models/res_lang.py）
        "data/lang_zh_tw_activation.xml",
    ],
    "assets": {
        "web.assets_backend": [
            "woow_paas_platform/static/src/scss/main.scss",
            # 後續在此添加 JS 和 XML 元件
            # 'woow_paas_platform/static/src/components/**/*',
            # 'woow_paas_platform/static/src/services/**/*',
        ],
        "woow_paas_platform.assets_paas": [
            # Odoo 18 standalone OWL stack: no ERP/website startup services.
            # Keep this SCSS order: helpers -> variables -> Bootstrap -> core.
            ("include", "web._assets_helpers"),
            "web/static/src/scss/pre_variables.scss",
            "web/static/lib/bootstrap/scss/_variables.scss",
            # Bootstrap 5.3 utilities consume theme-colors-rgb from _maps.
            "web/static/lib/bootstrap/scss/_variables-dark.scss",
            "web/static/lib/bootstrap/scss/_maps.scss",
            ("include", "web._assets_bootstrap"),
            ("include", "web._assets_core"),
            # SCSS - explicit load order to ensure variables are loaded first
            "woow_paas_platform/static/src/paas/styles/00_variables.scss",
            "woow_paas_platform/static/src/paas/styles/10_base.scss",
            "woow_paas_platform/static/src/paas/styles/20_layout.scss",
            "woow_paas_platform/static/src/paas/styles/30_components.scss",
            "woow_paas_platform/static/src/paas/styles/pages/*.scss",
            "woow_paas_platform/static/src/paas/styles/99_main.scss",
            # Component-specific SCSS (after base styles)
            "woow_paas_platform/static/src/paas/styles/components/*.scss",
            "woow_paas_platform/static/src/paas/components/**/*.scss",
            "woow_paas_platform/static/src/paas/pages/**/*.scss",
            # Third-party libraries (must load before app JS)
            "woow_paas_platform/static/src/paas/lib/purify.min.js",
            "woow_paas_platform/static/src/paas/lib/marked.min.js",
            # JS and XML files
            "woow_paas_platform/static/src/paas/**/*.js",
            "woow_paas_platform/static/src/paas/**/*.xml",
        ],
        "web.assets_frontend": [
            "woow_paas_platform/static/src/paas/lib/purify.min.js",
            "woow_paas_platform/static/src/paas/lib/marked.min.js",
            "woow_paas_platform/static/src/paas/services/rpc.js",
            "woow_paas_platform/static/src/paas/services/markdown_parser.js",
            "woow_paas_platform/static/src/paas/services/ai_service.js",
            "woow_paas_platform/static/src/paas/services/ai_stream.js",
            "woow_paas_platform/static/src/paas/services/agent_pod_state.js",
            "woow_paas_platform/static/src/paas/services/html_sanitize.js",
            "woow_paas_platform/static/src/portal/**/*",
        ],
    },
    # Note: Python dependencies are defined in requirements.txt (single source of truth).
    # pre_init_hook reads requirements.txt and auto-installs missing packages.
    # Helm chart and Dockerfile.dev also read from the same file.
    # external_dependencies: skill_bundle_validation mirrors the runtime's
    # yaml.safe_load frontmatter parse; declaring 'yaml' makes the module fail at
    # install time (not at first skill upload) if PyYAML is somehow absent.
    "external_dependencies": {"python": ["yaml"]},
    "pre_init_hook": "pre_init_hook",
    "post_init_hook": "post_init_hook",
    "uninstall_hook": "uninstall_hook",
    "installable": True,
    "application": True,
    "auto_install": False,
    "license": "LGPL-3",
}
