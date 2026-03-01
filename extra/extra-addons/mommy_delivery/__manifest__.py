# -*- coding: utf-8 -*-
{
    'name': "Delivery",

    'summary': """
        Delivery Extension Powered by Odoomommy Network Technology.
    """,

    'description': """
        1. Add Length Witdh and Height attributes on Product Template.
        2. Delivery Carrier Comparison
    """,

    'author': "Qingdao Ohm Network Technology Co., Ltd.",
    'website': "https://www.odoomommy.com",

    # Categories can be used to filter modules in modules listing
    # Check https://github.com/odoo/odoo/blob/16.0/odoo/addons/base/data/ir_module_category_data.xml
    # for the full list
    'category': 'tools',
    'version': '18.0.1.0',

    # any module necessary for this one to work correctly
    'depends': ['stock_delivery','mommy_product'],

    # always loaded
    'data': [
        'security/data.xml',
        'security/ir.model.access.csv',
        'views/views.xml',
        'views/templates.xml',
        'views/delivery.xml',
        'wizard/compare_wizard.xml'
    ],
    'images': ["images/mommy.jpg"],
    # only loaded in demonstration mode
    'demo': [
        'demo/demo.xml',
    ],
    "application": True,
    "price": 20,
    "currency": "EUR"
}
