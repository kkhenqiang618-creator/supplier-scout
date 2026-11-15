-- 0002: store JSON documents as jsonb and ISO timestamps as timestamptz.
--
-- Only converts columns that are still TEXT, so re-running is a no-op.  No column,
-- row or default is lost: a column's TEXT default is dropped for the type change
-- (Postgres cannot cast e.g. `'{}'` to jsonb implicitly) and put back as jsonb with
-- the same empty document, empty JSON text becomes that empty document, and empty
-- timestamps become NULL.  Run inside a single transaction (-1) so a failure
-- leaves the old schema intact.
DO $$
DECLARE
    item record;
BEGIN
    FOR item IN
        SELECT * FROM (VALUES
            ('suppliers','contacts','{}','{}'),
            ('suppliers','offers','[]','[]'),
            ('suppliers','sources','[]','[]'),
            ('suppliers','warnings','[]','[]'),
            ('suppliers','contact_result','{}','{}'),
            ('history','offers','[]',NULL)
        ) AS t(table_name,column_name,empty_value,default_value)
    LOOP
        IF EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_schema='scout_private' AND table_name=item.table_name
              AND column_name=item.column_name AND data_type='text'
        ) THEN
            IF item.default_value IS NOT NULL THEN
                -- The TEXT default cannot be cast to jsonb while the column is TEXT.
                EXECUTE format(
                    'ALTER TABLE scout_private.%I ALTER COLUMN %I DROP DEFAULT',
                    item.table_name,item.column_name);
            END IF;
            EXECUTE format(
                'ALTER TABLE scout_private.%I ALTER COLUMN %I TYPE jsonb USING (CASE WHEN btrim(%I)=%L THEN %L::jsonb ELSE %I::jsonb END)',
                item.table_name,item.column_name,item.column_name,'',item.empty_value,item.column_name);
            IF item.default_value IS NOT NULL THEN
                EXECUTE format(
                    'ALTER TABLE scout_private.%I ALTER COLUMN %I SET DEFAULT %L::jsonb',
                    item.table_name,item.column_name,item.default_value);
            END IF;
        END IF;
    END LOOP;
END $$;

DO $$
DECLARE
    item record;
BEGIN
    FOR item IN
        SELECT * FROM (VALUES
            ('suppliers','checked_at'),
            ('suppliers','created_at'),
            ('suppliers','contact_checked_at'),
            ('events','created_at'),
            ('history','created_at'),
            ('drafts','created_at')
        ) AS t(table_name,column_name)
    LOOP
        IF EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_schema='scout_private' AND table_name=item.table_name
              AND column_name=item.column_name AND data_type='text'
        ) THEN
            EXECUTE format(
                'ALTER TABLE scout_private.%I ALTER COLUMN %I DROP DEFAULT',
                item.table_name,item.column_name);
            EXECUTE format(
                'ALTER TABLE scout_private.%I ALTER COLUMN %I TYPE timestamptz USING NULLIF(btrim(%I),%L)::timestamptz',
                item.table_name,item.column_name,item.column_name,'');
        END IF;
    END LOOP;
END $$;

INSERT INTO scout_private.schema_migrations(version) VALUES ('0002_json_and_time_columns') ON CONFLICT DO NOTHING;
