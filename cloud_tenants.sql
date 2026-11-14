-- Existing private-schema data is preserved; pre-isolation shared data belongs to 小姜.
-- owner defaults to the logged-in account only: a write without a user context now fails
-- instead of silently landing in 小姜's rows.
DO $block$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['suppliers','settings','events','history','drafts','cloud_jobs'] LOOP
    EXECUTE format('ALTER TABLE scout_private.%I ADD COLUMN IF NOT EXISTS owner text',t);
    EXECUTE format('UPDATE scout_private.%I SET owner = %L WHERE owner IS NULL',t,'小姜');
    EXECUTE format('ALTER TABLE scout_private.%I ALTER COLUMN owner SET DEFAULT NULLIF(current_setting(''scout.username'',true),'''')',t);
    EXECUTE format('ALTER TABLE scout_private.%I ALTER COLUMN owner SET NOT NULL',t);
    EXECUTE format('ALTER TABLE scout_private.%I ENABLE ROW LEVEL SECURITY',t);
    EXECUTE format('DROP POLICY IF EXISTS app_access ON scout_private.%I',t);
    EXECUTE format('CREATE POLICY app_access ON scout_private.%I TO scout_app USING ((SELECT current_setting(''scout.system'',true))=''on'' OR owner=(SELECT current_setting(''scout.username'',true))) WITH CHECK ((SELECT current_setting(''scout.system'',true))=''on'' OR owner=(SELECT current_setting(''scout.username'',true)))',t);
    EXECUTE format('CREATE INDEX IF NOT EXISTS %I ON scout_private.%I(owner)',t||'_owner_idx',t);
  END LOOP;
END
$block$;
UPDATE scout_private.settings SET owner=substr(key,length('model-service:user:')+1) WHERE key LIKE 'model-service:user:%';
UPDATE scout_private.cloud_jobs SET owner=COALESCE(NULLIF(state->>'requested_by',''),'小姜');
INSERT INTO scout_private.settings(key,value,owner) SELECT 'rules:user:小姜',value,'小姜' FROM scout_private.settings WHERE key='rules' ON CONFLICT(key) DO NOTHING;
ALTER TABLE scout_private.suppliers DROP CONSTRAINT IF EXISTS suppliers_url_mode_key;
CREATE UNIQUE INDEX IF NOT EXISTS suppliers_owner_url_mode ON scout_private.suppliers(owner,url,mode);
REVOKE ALL ON SCHEMA scout_private FROM PUBLIC,anon,authenticated;
