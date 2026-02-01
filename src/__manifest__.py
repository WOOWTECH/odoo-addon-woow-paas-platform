{
    'name': 'Woow PaaS Platform',
    'version': '18.0.1.0.0',
    'category': 'WOOW',
    'summary': 'Woow PaaS Platform - Base Module',
    'description': '''
        Woow PaaS Platform 的基礎模組。
        提供 PaaS 平台的核心功能。
    ''',
    'author': 'Woow',
    'website': '',
    'depends': ['base', 'web'],
    'data': [
        'security/ir.model.access.csv',
        'security/ir_rules.xml',
        'views/res_config_settings_views.xml',
        'views/menu.xml',
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
            # SCSS/JS/XML - use number prefixes for load order (00_, 10_, 20_, ...)
            'woow_paas_platform/static/src/paas/**/*',
        ],
    },
    'installable': True,
    'application': True,
    'auto_install': False,
    'license': 'LGPL-3',
}
