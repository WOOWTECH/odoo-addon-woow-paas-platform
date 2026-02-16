{
    'name': "AI Automation",
    'summary': """
Integrate AI Assistants with Odoo Automation Rules and Scheduled Actions.
Allow automation rules and scheduled actions to execute AI actions with custom prompts.
Can use other AI models like ChatGPT, Gemini, Claude, etc through connector modules.
""",
    'sequence': 160,
    'description': """
Integrate AI Assistants with Odoo Automation Rules and Scheduled Actions.
Allow automation rules and scheduled actions to execute AI actions with custom prompts.
Can use other AI models like ChatGPT, Gemini, Claude, etc through connector modules.
""",
    'author': "GT Apps",
    'support': 'gt.apps.odoo@gmail.com',
    'live_test_url': 'https://ai-demo.gt-apps.top',
    'category': 'Productivity/AI',
    'version': '0.1.1',
    'depends': ['ai_base_gt', 'base_automation'],
    'data': [
        'views/ir_actions_server_views.xml',
        'views/ir_cron_views.xml',
        'views/ai_thread_views.xml',
    ],
    'demo': [],
    'images': ['static/description/banner.jpg'],
    'installable': True,
    'application': False,
    'auto_install': False,
    'license': 'OPL-1',
    'price': 63.9,
    'currency': 'USD',
}
