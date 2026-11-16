-- 0003: add suppliers.archived_at for the reversible "hide" action.
--
-- Archiving replaces deleting a record: the row keeps its quotes, contacts,
-- follow-up state and e-mail draft, is simply hidden from the workspace lists,
-- and can be restored.  The column is additive and idempotent, so running the
-- file twice (or on a database created from the newer cloud_schema.sql) is a
-- no-op.  Run inside a single transaction (-1).
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema='scout_private' AND table_name='suppliers'
          AND column_name='archived_at'
    ) THEN
        ALTER TABLE scout_private.suppliers ADD COLUMN archived_at TEXT;
    END IF;
END $$;

INSERT INTO scout_private.schema_migrations(version) VALUES ('0003_archived_at') ON CONFLICT DO NOTHING;
