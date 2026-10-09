{
    'name': 'Fake POS Enhancement',
    'version': '18.0.1.2.0',
    'summary': 'standalone-odoo-migrate 測試用客製 addon（仿 pos_self_order_enhancement）',
    'depends': ['point_of_sale'],
    'external_dependencies': {'python': ['pycryptodomex']},
    'data': ['security/ir.model.access.csv', 'data/fake_pos_note_data.xml'],
    'license': 'LGPL-3',
    'installable': True,
}
