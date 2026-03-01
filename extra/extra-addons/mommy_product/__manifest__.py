# -*- coding: utf-8 -*-
{
    'name': "Product",

    'summary': """
        Mommy Product Module for Odoo
    """,

    'description': """
        This module extends the product functionality in Odoo, providing additional features and enhancements for product management.
        It includes custom fields, views, and business logic to improve the product management experience in Odoo.
        Key features include:
        - Custom product attributes
        - Enhanced product views
        - Integration with other modules for a seamless user experience
    """,

    'author': "Qingdao Ohm Network Technology Co., Ltd.",
    'website': "https://www.odoomommy.com",

    # Categories can be used to filter modules in modules listing
    # Check https://github.com/odoo/odoo/blob/15.0/odoo/addons/base/data/ir_module_category_data.xml
    # for the full list
    'category': 'tools',
    'version': '18.0.1.0',

    # any module necessary for this one to work correctly
    'depends': ['mommy_base','product'],

    'assets':{
        'web.assets_backend':[]
    },
    
    'images': ["images/mommy.png"],

    # always loaded
    'data': [
        # 'security/ir.model.access.csv',
        'views/views.xml',
        'views/templates.xml',
    ],
    # only loaded in demonstration mode
    'demo': [
        'demo/demo.xml',
    ],
    "application": True,
    "price": 10,
    "currency": "EUR"
}
