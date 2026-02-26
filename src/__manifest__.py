{
    'name': 'Woow PaaS Platform',
    'version': '18.0.1.0.3',
    'category': 'WOOW',
    'summary': 'Woow PaaS Platform - Base Module',
    'description': '''
        Woow PaaS Platform 的基礎模組。
        提供 PaaS 平台的核心功能。
    ''',
    'author': 'Woow',
    'website': '',
    'depends': ['base', 'web', 'project', 'mail', 'bus', 'odoo_ai_assistant_chatgpt_connector'],
    'data': [
        'security/ir.model.access.csv',
        'security/ir_rules.xml',
        'data/cloud_app_templates.xml',
        'views/menu.xml',
        'data/ai_assistant_data.xml',
        'views/project_task_views.xml',
        'views/ai_config_views.xml',
        'views/mcp_server_views.xml',
        'views/res_config_settings_views.xml',
        'views/paas_app.xml',
    ],
    'assets': {
        'web.assets_backend': [
            'woow_paas_platform/static/src/scss/main.scss',
            # 後續在此添加 JS 和 XML 元件
            # 'woow_paas_platform/static/src/components/**/*',
            # 'woow_paas_platform/static/src/services/**/*',
        ],
        'woow_paas_platform.assets_paas': [
            ('include', 'web.assets_backend'),
            # SCSS - explicit load order to ensure variables are loaded first
            'woow_paas_platform/static/src/paas/styles/00_variables.scss',
            'woow_paas_platform/static/src/paas/styles/10_base.scss',
            'woow_paas_platform/static/src/paas/styles/20_layout.scss',
            'woow_paas_platform/static/src/paas/styles/30_components.scss',
            'woow_paas_platform/static/src/paas/styles/pages/*.scss',
            'woow_paas_platform/static/src/paas/styles/99_main.scss',
            # Component-specific SCSS (after base styles)
            'woow_paas_platform/static/src/paas/styles/components/*.scss',
            'woow_paas_platform/static/src/paas/components/**/*.scss',
            'woow_paas_platform/static/src/paas/pages/**/*.scss',
            # Third-party libraries (must load before app JS)
            'woow_paas_platform/static/src/paas/lib/purify.min.js',
            'woow_paas_platform/static/src/paas/lib/marked.min.js',
            # JS and XML files
            'woow_paas_platform/static/src/paas/**/*.js',
            'woow_paas_platform/static/src/paas/**/*.xml',
        ],
    },
    # Note: Python dependencies (langchain-openai, langchain-core) are auto-installed by pre_init_hook
    # Do not use external_dependencies as it blocks installation before hook runs
    'pre_init_hook': 'pre_init_hook',
    'installable': True,
    'application': True,
    'auto_install': False,
    'license': 'LGPL-3',
}
