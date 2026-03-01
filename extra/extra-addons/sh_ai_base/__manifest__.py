{
    "name": "AI Base",
    "author": "Softhealer Technologies",
    "website": "https://www.softhealer.com",
    "support": "support@softhealer.com",
    "category": "Extra Tools",
    "license": "OPL-1",
    "summary": "ai assistance odoo ai integration chatgpt odoo gemini odoo openchat odoo llm odoo integration ai chatbot odoo ai assistant smart ai odoo ai tools odoo chat assistant odoo ai automation odoo ai productivity odoo openai integration ai model integration odoo generative ai odoo artificial intelligence odoo ai features ai chat app odoo ai prompt odoo ai workspace odoo ai manager rights odoo ai response generator odoo llm management odoo ai system odoo ai question answer odoo ai helper odoo ai support odoo ai chat window odoo ai interface odoo ai models access control odoo business ai odoo ai assistant app odoo ai tips odoo chat delete feature odoo ai suggestions odoo ai helpdesk odoo smart chat integration odoo conversational ai",
    "description": """This is the only base app for AI apps.""",
    "version": "0.0.2",
    'depends': ['web','mail'],
    'external_dependencies': {
        'python': [
            'openai',
            'google-genai',
        ]
    },
    'data': [
        'security/sh_ai_groups.xml',
        'security/ir.model.access.csv',
        'data/demo_llm_provider.xml',
        'views/sh_ai_llm_views.xml',
    ],
    
    'assets': {
        'web.assets_backend': [
            "sh_ai_base/static/src/js/boolean_toggle.js"
        ],
    },
    'installable': True,
    'application': True,
    "images": ["static/description/background.png", ],
    "price": 1,
    "currency": "EUR"
}
