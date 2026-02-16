{
    "name": "InsightMate – AI Insight Assistant - Talk to Your Data",
    "author": "Softhealer Technologies",
    "website": "https://www.softhealer.com",
    "support": "support@softhealer.com",
    "category": "Extra Tools",
    "license": "OPL-1",
    "summary": "AI Analyst real-time Odoo data insights with an AI Analyst real time Odoo data insights with an AI Analyst AI assistance chatbot Odoo AI agent Odoo chatgpt integration gemini integration Artificial Intelligence agent Odoo AI Analyst Gemini API OpenAI API ChatGPT API Langchain Odoo Analysis Odoo data analysis odoo sales analysis odo purchase analysis project analysis invoice analysis odoo reports odoo dashboard reporting sales reporting profitability analysis ai assistance chatgpt integration odoo Copilot GPT-4o Gemini Pro Claude 3.5 Sonnet  Llama 3 OpenRouter Natural Language Query (NLQ) Text-to-SQL AI Dashboard  Semantic Search RAG (Retrieval-Augmented Generation) Chat with Odoo Data AI for CFO Sales Intelligence AI Predictive Accounting Inventory Forecasting AI Smart Lead Scoring Automated Reporting AI Assistant for Odoo Discuss AI Bot AI Workflow Automation Virtual Assistant sales dashboard with AI ChatRoom Odoo Bot  instant charts Graph PDF Reports odoo chat AI Chatbot AIi chat personal data analytics assistant Query sales CRM Questions  AI prompts data exploration data analysis odoo analysis LMM providers big query looker studio AI Analyser analyse your data improve sales improve support Smart Access Control Access management Compatible with Odoo Access Rights Dedicated AI Chat Interface Dark Mode Light Mode Performance Insights Cross-Module Integration System Prompt Intelligence Intelligent Data Understanding AI Conversation Custom Development LLM provider Odoo AI Analyst Chatbot LLM Integration Inventory Forecasting Ai Center ai assistance odoo AI assistant Odoo ai integration chatgpt odoo gemini odoo openchat odoo llm odoo integration ai chatbot odoo ai assistant smart ai odoo ai tools odoo chat assistant odoo ai automation odoo ai productivity odoo openai integration ai model integration odoo generative ai odoo artificial intelligence odoo ai features ai chat app odoo ai prompt odoo ai workspace odoo ai manager rights odoo ai response generator odoo llm management odoo ai system odoo ai question answer odoo ai helper odoo ai support odoo ai chat window odoo ai interface odoo ai models access control odoo business ai odoo ai assistant app odoo ai tips odoo chat delete feature odoo ai suggestions odoo ai helpdesk odoo smart chat integration odoo conversational ai Natural language Processing Odoo Integration AI chat bot Odoo Reports Odoo AI Reports LLM Odoo Natural LLM Odoo chat share chat export chat import chat history management chat backup restore chat conversation sharing chat data export chat data import AI chat Import export AI chat Export Share AI chat share AI chat session chat JSON export chat restore feature chat collaboration chat archive AI chat tools conversation portability chat transfer chat sharing link export AI chat history import AI conversations chat continuity chat migration chat save load chat data management chat recovery feature AI assistant chat export AI assistant chat import AI assistant chat share chat system Odoo AI chat features JSON chat export AI chat sharing AI chat history export AI chat history Import AI chat history export AI chat JSON export AI conversation sharing AI chat collaboration AI chat restore AI chat session management Odoo AI chat features AI chat continuity AI chat data portability AI chat file upload AI chat download AI chat reuse AI chat archive AI assistant chat management",
    "description": """Meet AI Assistance - your smart business companion that redefines how you explore, analyze, and understand your data. Just ask in natural language and instantly get data, charts, tables, dashboards, and insights - all powered by intelligent automation. It helps you improve efficiency, enhance service quality, analyze data faster, and streamline your processes effortlessly. Let your data speak back to you with powerful insights and actionable guidance!""",
    "version": "18.0.3.0.0",
    'depends': ['sh_ai_base'],
    'external_dependencies': {
        'python': ['markdown'],
    },
    'data': [
        'security/ir.model.access.csv',
        'security/sh_ai_chat_rules.xml',
        'views/dashboard_action.xml',
        'views/sh_ai_llm_views.xml',
        'views/sh_ai_chat_session_views.xml',
        'views/sh_ai_chat_message_views.xml',
        'views/share_template.xml',
        'views/sh_ai_dashboard_menus.xml',
        'views/webclient_templates.xml',
    ],
    'assets': {
        'web.assets_backend': [
            # Third-party libraries (non-modules)
            'sh_ai_assistance/static/lib/marked/marked.min.js',
            'sh_ai_assistance/static/lib/color_scheme_detector.js',
            # Module files (Odoo modules)
            'sh_ai_assistance/static/src/**/*.scss',
            'sh_ai_assistance/static/src/**/*.js',
            'sh_ai_assistance/static/src/**/*.xml',
        ],
        'web.assets_frontend': [
            'sh_ai_assistance/static/src/scss/shared_chat_page.scss',
        ],
    },
    'installable': True,
    'application': True,
    "images": ["static/description/background.gif", ],
    "price": 124.90,
    "currency": "EUR"
}
