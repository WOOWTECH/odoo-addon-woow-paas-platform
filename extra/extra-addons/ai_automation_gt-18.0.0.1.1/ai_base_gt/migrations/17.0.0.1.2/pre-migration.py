def migrate(cr, version):
    """
    Migrate ai.data.source text data to ai.data.item if not exists.
    """
    cr.execute("""
        INSERT INTO ai_data_item (data_source_id, source, data)
        SELECT id, 'text_' || id, text FROM ai_data_source WHERE type = 'text'
        ON CONFLICT (data_source_id, source) DO NOTHING;
    """)
