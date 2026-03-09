def migrate(cr, version):
    # Fill new model of ai.msg.attachment from old attachments of ai.msg
    cr.execute("""
        INSERT INTO ai_message_attachment (
            create_date,
            create_uid,
            write_date,
            write_uid,
            message_id,
            thread_id,
            attachment_id
        )
        SELECT 
            att.create_date,
            att.create_uid,
            att.write_date,
            att.write_uid,
            msg.id,
            msg.thread_id,
            att.id
        FROM ai_message_ir_attachment_rel_bak rel
        JOIN ai_message msg ON rel.ai_message_id = msg.id
        JOIN ir_attachment att ON rel.ir_attachment_id = att.id
    """)
    # Drop backup table
    cr.execute("""
        DROP TABLE ai_message_ir_attachment_rel_bak;
    """)
