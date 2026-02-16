# -*- coding: utf-8 -*-
{
    'name': "SF Express",

    'summary': """顺丰速运""",

    'description': """
        顺丰速运

        使用顺丰速运完成发货和物流跟踪,面单打印
    """,

    'author': "Qingdao Ohm Network Technology Co., Ltd.",
    'website': "https://www.odoomommy.com",

    # Categories can be used to filter modules in modules listing
    # Check https://github.com/odoo/odoo/blob/16.0/odoo/addons/base/data/ir_module_category_data.xml
    # for the full list
    'category': 'delivery',
    'version': '18.0.1.0',

    # any module necessary for this one to work correctly
    'depends': ['mommy_delivery'],

    # always loaded
    'data': [
        'security/ir.model.access.csv',
        'security/mommy.sf.template.csv',
        "security/data.xml",
        'views/views.xml',
        'views/delivery.xml',
        'views/partner.xml'
    ],
    "external_dependencies": {
        "python": ["sf"]
    },
    # only loaded in demonstration mode
    'demo': [
        'demo/demo.xml',
    ],
    "images":["images/sf.jpg"],
    "price": "80",
    "currency": 'EUR',
    "application": True,
    'support': 'kevin@odoomommy.com',
    "license": "OPL-1",
}
