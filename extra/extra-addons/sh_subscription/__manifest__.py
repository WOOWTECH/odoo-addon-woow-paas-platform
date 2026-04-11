# -*- coding: utf-8 -*-
# Part of Softhealer Technologies.
{
    "name": "Subscription Management System",
    # 繁體中文: 訂閱管理系統

    "author": "Softhealer Technologies",

    "website": "https://www.softhealer.com",

    "version": "0.0.4",

    "license": "OPL-1",

    "support": "support@softhealer.com",

    "category": "Extra Tools",

    "summary": "訂閱管理系統 - 自訂訂閱方案、訂閱型產品、網站訂閱、Portal 訂閱管理、銷售訂閱頁面",
    # English: Customized Subscription Plans Subscription Based Products Subscription Odoo Subscription Manage Subscription Website Subscription Portal Product Subscription Plan Manage Sales Subscription Page Subscription Pricing Page on Website

    "description": """
訂閱管理系統

功能特色：
- 建立多種訂閱方案（每日、每週、每月、每年）
- 透過郵件範本發送訂閱提醒給客戶
- 設定訂閱週期自動續訂
- 客戶可透過 Portal 自行管理訂閱方案
- 彈性的帳單週期設定
- 自動到期提醒郵件
- 完整的訂閱報表功能

Subscription Management System

Are you looking for a subscription management system? We provides the subscription system with subscription plans. You can create subscription plans like daily, weekly, monthly & yearly. You can send subscription reminders to customers using email templates. You can set recurring periods on subscriptions to renew it. Customer can manage their subscription plans for themselves directly from portal. This module also allows businesses to create and manage flexible subscription plans, set customizable billing cycles, and automate email alerts for expirations. Customers can manage their plans via a self-service portal, while detailed reporting features provide valuable insights for strategic decision-making.
""",
    
    "depends": ['base_setup', 'sale_management'],
    
    "data": [
        'security/sh_subscription_security.xml',
        'security/ir.model.access.csv',
        'data/ir_sequence_data.xml',
        'data/sh_subscription_demo.xml',
        'wizard/sh_subscription_cancle_wizard_views.xml',
        'wizard/sh_subscription_renewed_wizard_views.xml',
        'wizard/sh_subscription_product_wizard_views.xml',
        'views/res_config_settings_views.xml',
        'views/sh_subcription_plan_views.xml',
        'data/email_templates.xml',
        'views/sh_subcription_subcription_views.xml',
        'data/subscription_cron.xml',
        'views/sh_subscription_reason_views.xml',
        'views/product_product_views.xml',
        'views/sale_order_views.xml',
        'views/account_move_views.xml',
        'views/sh_reminder_template_views.xml',
        'views/sh_subscription_report_views.xml',
        'views/portal_templates.xml',
    ],
    'assets': {
        'web.assets_frontend': [
            'sh_subscription/static/src/css/portal_brand.css',
            'sh_subscription/static/src/js/subscription_portal.js',
        ],
    },
    "auto_install": False,
    "installable": True,
    "application": True,
    "images": ["static/description/background.png", ],
    "price": 70,
    "currency": "EUR"
}
