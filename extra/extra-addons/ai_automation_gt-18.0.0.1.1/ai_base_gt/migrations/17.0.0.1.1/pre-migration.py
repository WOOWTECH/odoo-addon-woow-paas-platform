def migrate(cr, version):
    """
    Convert func_call and func_result columns from JSON/JSONB to TEXT in ai_message table.
    """
    for col in ("func_call", "func_result"):
        cr.execute(f'''
            DO $$
            BEGIN
                IF (SELECT data_type FROM information_schema.columns
                    WHERE table_name='ai_message' AND column_name='{col}') IN ('json', 'jsonb') THEN
                    ALTER TABLE ai_message
                        ALTER COLUMN {col} TYPE text
                        USING {col}::text;
                END IF;
            END$$;
        ''')
