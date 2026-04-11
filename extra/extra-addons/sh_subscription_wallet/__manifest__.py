# -*- coding: utf-8 -*-
{
    "name": "訂閱錢包 - eWallet 點數與信用卡扣款",
    "summary": "eWallet 點數錢包整合訂閱週期扣款，支援 ECPay 儲值與信用卡 Token 扣款",
    "description": """
訂閱錢包 - eWallet 點數與信用卡扣款
====================================

功能特色：
- 點數錢包：使用 Odoo Loyalty 點數系統
- ECPay 儲值：透過 ECPay 線上付款購買點數
- 信用卡綁定：使用 ECPay 定期定額 Token 機制
- 智慧扣款：訂閱到期時先扣點數，不足部分自動刷綁定的信用卡
- 彈性週期：支援每日、每週、每月、每年扣款
- 交易記錄：完整的點數異動歷史
- Portal 頁面：客戶可查看餘額、儲值、管理信用卡

扣款流程：
1. 檢查點數餘額
2. 點數充足 → 全額扣點數
3. 點數不足 → 扣完點數 + 刷卡補差額
4. 扣款失敗 → 標記訂閱異常，發送通知
    """,
    "version": "18.0.1.1.0",
    "category": "Sales/Subscription",
    "author": "WOOWTECH",
    "website": "https://www.woowtech.com",
    "license": "LGPL-3",
    "depends": [
        "loyalty",
        "sale_loyalty",
        "sh_subscription",
    ],
    "data": [
        "security/ir.model.access.csv",
        "data/loyalty_program_data.xml",
        "data/cron_data.xml",
        "views/subscription_plan_views.xml",
        "views/subscription_views.xml",
        "views/res_partner_views.xml",
        "views/wallet_transaction_views.xml",
        "views/portal_templates.xml",
        "views/wallet_topup_templates.xml",
    ],
    "assets": {
        "web.assets_frontend": [
            "sh_subscription_wallet/static/src/js/wallet.js",
        ],
    },
    "installable": True,
    "application": False,
    "auto_install": False,
}
