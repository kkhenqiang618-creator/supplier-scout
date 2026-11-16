-- 0003: the browser-worker process reports itself here every 10 seconds, so the page can show
-- whether the daemon is running and where it stopped. The row carries no member business data,
-- so this table has no owner column and no row-level security; the worker writes it in the
-- system context, and every member may read it.
-- Safe to re-run: the table, the grant and the version row are all created only when missing.
CREATE TABLE IF NOT EXISTS scout_private.cloud_worker_status(
    id TEXT PRIMARY KEY,
    started_at TIMESTAMPTZ NOT NULL,
    heartbeat_at TIMESTAMPTZ NOT NULL,
    state TEXT NOT NULL DEFAULT 'unknown',
    job_id TEXT NOT NULL DEFAULT '',
    job_owner TEXT NOT NULL DEFAULT '',
    site TEXT NOT NULL DEFAULT '',
    error_text TEXT NOT NULL DEFAULT '',
    version TEXT NOT NULL DEFAULT ''
);

DO $block$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.role_table_grants
        WHERE table_schema='scout_private' AND table_name='cloud_worker_status' AND grantee='scout_app'
    ) THEN
        GRANT SELECT,INSERT,UPDATE,DELETE ON scout_private.cloud_worker_status TO scout_app;
    END IF;
END $block$;

DO $block$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_schema='scout_private' AND table_name='schema_migrations'
    ) THEN
        INSERT INTO scout_private.schema_migrations(version) VALUES ('0003_worker_status') ON CONFLICT DO NOTHING;
    END IF;
END $block$;
