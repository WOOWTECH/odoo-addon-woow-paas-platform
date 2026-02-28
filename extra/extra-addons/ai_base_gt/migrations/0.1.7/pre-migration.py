def migrate(cr, version):
    # Backup table ai_message_ir_attachment_rel for post-migration
    cr.execute("""
        ALTER TABLE ai_message_ir_attachment_rel RENAME TO ai_message_ir_attachment_rel_bak;
    """)
