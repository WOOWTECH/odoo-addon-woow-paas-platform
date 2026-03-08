{
    'name': "AI ChatGPT Connector",
    'summary': "ChatGPT integration for AI Complete Suite",
    'description': """
This module extends the AI Complete Suite to support ChatGPT API integration.
    """,
    'author': "GT Apps",
    'support': 'gt.apps.odoo@gmail.com',
    'live_test_url': 'https://ai-demo.gt-apps.top',
    'category': 'Productivity/AI',
    'version': '0.1',
    'depends': ['ai_base_gt'],
    'external_dependencies': {
        'python': ['openai>=1.0.0'],
    },
    'data': [
        'data/ai_config_data.xml',
        'views/ai_config_views.xml',
    ],
    'images': ['static/description/banner.jpg'],
    'installable': True,
    'auto_install': False,
    'license': 'OPL-1',
    'price': 0.0,
    'currency': 'USD',
}
