from odoo import api, SUPERUSER_ID


def migrate(cr, version):
    """
    Migrate assistant-data_source relation from ai_assistant_data_source_rel
    to ai.assistant.data.source, then drop old table.
    """
    env = api.Environment(cr, SUPERUSER_ID, {})
    Model = env['ai.assistant.data.source']
    cr.execute("SELECT assistant_id, data_source_id FROM ai_assistant_data_source_rel")
    for assistant_id, data_source_id in cr.fetchall():
        Model.create({
            'assistant_id': assistant_id,
            'data_source_id': data_source_id,
        })

    cr.execute("DROP TABLE IF EXISTS ai_assistant_data_source_rel CASCADE;")
