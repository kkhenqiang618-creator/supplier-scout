-- Private application schema; never exposed through Supabase's public Data API.
CREATE SCHEMA IF NOT EXISTS scout_private;
REVOKE ALL ON SCHEMA scout_private FROM PUBLIC, anon, authenticated;
CREATE TABLE scout_private.suppliers(id BIGSERIAL PRIMARY KEY,name TEXT NOT NULL,url TEXT NOT NULL,mode TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'pending',reason TEXT NOT NULL DEFAULT '',contacts TEXT NOT NULL DEFAULT '{}',offers TEXT NOT NULL DEFAULT '[]',sources TEXT NOT NULL DEFAULT '[]',warnings TEXT NOT NULL DEFAULT '[]',notes TEXT NOT NULL DEFAULT '',checked_at TEXT,created_at TEXT NOT NULL,contact_status TEXT NOT NULL DEFAULT 'not_started',contact_checked_at TEXT,contact_result TEXT NOT NULL DEFAULT '{}',UNIQUE(url,mode));
CREATE TABLE scout_private.settings(key TEXT PRIMARY KEY,value TEXT);
CREATE TABLE scout_private.events(id BIGSERIAL PRIMARY KEY,supplier_id BIGINT,mode TEXT,message TEXT,created_at TEXT);
CREATE TABLE scout_private.history(id BIGSERIAL PRIMARY KEY,supplier_id BIGINT,mode TEXT,offers TEXT,created_at TEXT);
CREATE TABLE scout_private.drafts(id BIGSERIAL PRIMARY KEY,supplier_id BIGINT UNIQUE,subject TEXT,body TEXT,created_at TEXT);
CREATE TABLE scout_private.cloud_jobs(id TEXT PRIMARY KEY,mode TEXT NOT NULL,scope TEXT NOT NULL,selection TEXT NOT NULL,supplier_ids JSONB NOT NULL,position INTEGER NOT NULL DEFAULT 0,status TEXT NOT NULL DEFAULT 'queued',state JSONB NOT NULL,cancel_requested BOOLEAN NOT NULL DEFAULT FALSE,lease_owner TEXT,lease_until TIMESTAMPTZ,created_at TIMESTAMPTZ NOT NULL DEFAULT now(),updated_at TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE UNIQUE INDEX cloud_jobs_one_active ON scout_private.cloud_jobs((true)) WHERE status IN ('queued','running','stopping');
CREATE INDEX cloud_jobs_ready ON scout_private.cloud_jobs(status,lease_until);
CREATE TABLE scout_private.sessions(token_hash TEXT PRIMARY KEY,username TEXT NOT NULL,expires_at DOUBLE PRECISION NOT NULL);
CREATE TABLE scout_private.login_attempts(key TEXT PRIMARY KEY,failures INTEGER NOT NULL DEFAULT 0,window_start DOUBLE PRECISION NOT NULL);

-- Provision the scout_app login role with an out-of-band random password first.
GRANT USAGE ON SCHEMA scout_private TO scout_app;
GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA scout_private TO scout_app;
GRANT USAGE,SELECT,UPDATE ON ALL SEQUENCES IN SCHEMA scout_private TO scout_app;
ALTER TABLE scout_private.suppliers ENABLE ROW LEVEL SECURITY;
CREATE POLICY app_access ON scout_private.suppliers TO scout_app USING (true) WITH CHECK (true);
ALTER TABLE scout_private.settings ENABLE ROW LEVEL SECURITY;
CREATE POLICY app_access ON scout_private.settings TO scout_app USING (true) WITH CHECK (true);
ALTER TABLE scout_private.events ENABLE ROW LEVEL SECURITY;
CREATE POLICY app_access ON scout_private.events TO scout_app USING (true) WITH CHECK (true);
ALTER TABLE scout_private.history ENABLE ROW LEVEL SECURITY;
CREATE POLICY app_access ON scout_private.history TO scout_app USING (true) WITH CHECK (true);
ALTER TABLE scout_private.drafts ENABLE ROW LEVEL SECURITY;
CREATE POLICY app_access ON scout_private.drafts TO scout_app USING (true) WITH CHECK (true);
ALTER TABLE scout_private.cloud_jobs ENABLE ROW LEVEL SECURITY;
CREATE POLICY app_access ON scout_private.cloud_jobs TO scout_app USING (true) WITH CHECK (true);
ALTER TABLE scout_private.sessions ENABLE ROW LEVEL SECURITY;
CREATE POLICY app_access ON scout_private.sessions TO scout_app USING (true) WITH CHECK (true);
ALTER TABLE scout_private.login_attempts ENABLE ROW LEVEL SECURITY;
CREATE POLICY app_access ON scout_private.login_attempts TO scout_app USING (true) WITH CHECK (true);
