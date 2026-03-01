# -*- coding: utf-8 -*-
{
    'name': "Base",

    'summary': """
        Basic feature powered by Odoomommy.com
    """,

    'description': """
        核心依赖模块
        
        Mommy Base Module provides basic features for Odoo applications, including security groups, access rights, and common views.
        It serves as a foundation for other modules, ensuring consistent security and functionality across the Odoo ecosystem.
    """,

    'author': "Qingdao Ohm Network Technology Co., Ltd.",
    'website': "https://www.qingsolution.com",

    # Categories can be used to filter modules in modules listing
    # Check https://github.com/odoo/odoo/blob/13.0/odoo/addons/base/data/ir_module_category_data.xml
    # for the full list
    'category': 'basic',
    'version': '18.0.1.6',

    # any module necessary for this one to work correctly
    'depends': ['sale','purchase','stock','auth_oauth'],

    # always loaded
    'data': [
        'security/ir.model.access.csv',
        'security/data.xml',
        'views/views.xml',
        'views/settings.xml',
        'views/pops.xml',
        'views/login_layout.xml',
        'views/mail_template.xml',
        'views/partner.xml'
    ],
    # only loaded in demonstration mode
    'demo': [
        'demo/demo.xml',
    ],
    'assets': {
        'web.assets_backend': [
            'mommy_base/static/src/xml/*',
        ],
    },
    "images":["images/mommy.png"],
    "price":"15",
    "currency": 'USD',
    "application": True,
    'support': 'kfx2007@163.com',
    "license": "OPL-1",
}
