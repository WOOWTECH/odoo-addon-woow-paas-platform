from odoo import api, SUPERUSER_ID


def migrate(cr, version):
    """
    Rename is_superuser to is_super_assistant
    """
    cr.execute("""
        ALTER TABLE ai_assistant RENAME COLUMN is_superuser TO is_super_assistant;
    """)
