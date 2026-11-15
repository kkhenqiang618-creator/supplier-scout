-- 0001: record which private-schema migrations have been applied.
-- Safe to re-run: both the table and the bookkeeping row are created only when missing.
CREATE TABLE IF NOT EXISTS scout_private.schema_migrations(
    version TEXT PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
INSERT INTO scout_private.schema_migrations(version) VALUES ('0001_schema_migrations') ON CONFLICT DO NOTHING;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.role_table_grants
        WHERE table_schema='scout_private' AND table_name='schema_migrations' AND grantee='scout_app'
    ) THEN
        GRANT SELECT ON scout_private.schema_migrations TO scout_app;
    END IF;
END $$;
