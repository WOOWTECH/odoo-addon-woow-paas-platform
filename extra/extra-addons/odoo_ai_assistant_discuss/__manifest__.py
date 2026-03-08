{
    'name': "AI Assistant for Discuss",
    'summary': """
Smart AI assistants for Odoo Discuss, combining AI capabilities with data sources and customizable context.
Can use other AI models like ChatGPT, Gemini, Claude, etc through connector modules.
""",
    'sequence': 150,
    'description': """
Smart AI assistants for Odoo Discuss, combining AI capabilities with data sources and customizable context.
Can use other AI models like ChatGPT, Gemini, Claude, etc through connector modules.
""",
    'author': "GT Apps",
    'support': 'gt.apps.odoo@gmail.com',
    'live_test_url': 'https://ai-demo.gt-apps.top',
    'category': 'Productivity/AI',
    'version': '0.1.0',
    'depends': ['mail', 'ai_base_gt'],
    'external_dependencies': {
        'python': ['markdownify>=0.11.0'],
    },
    'data': [],
    'demo': [],
    'assets': {
        'web.assets_backend': [
            'ai_mail_gt/static/src/**/*',
        ],
        'mail.assets_public': [
            'ai_mail_gt/static/src/core/common/**/*',
            'ai_mail_gt/static/src/core/public_web/**/*',
        ],
    },
    'images': ['static/description/banner.jpg'],
    'installable': True,
    'application': False,
    'auto_install': True,
    'license': 'OPL-1',
    'price': 99.9,
    'currency': 'USD',
}
