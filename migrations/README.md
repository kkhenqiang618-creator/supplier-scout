# Database migrations

Numbered, additive migrations.  Run them in order, once, before deploying the
release that needs them.

- `postgres/` — private Supabase schema (`scout_private`). Apply with
  `psql "$DATABASE_URL" -1 -f migrations/postgres/0001_schema_migrations.sql`.
- Local workspaces use SQLite and carry their own version number in
  `PRAGMA user_version`; `local_store.LOCAL_SCHEMA_VERSION` must match the
  highest local step and the store upgrades older files on open.

Rules for every script:

1. Only add or convert — never drop a table, delete rows or truncate data.
2. Idempotent: re-running a file must leave the same result (guards via
   `information_schema` / `IF NOT EXISTS`).
3. Record the version at the end of the file in
   `scout_private.schema_migrations`, so a re-run is a no-op.
4. Wrap with `-1` (single transaction) when applying by hand, so a failure
   rolls the whole file back.

`0002_json_and_time_columns.sql` converts the JSON documents to `jsonb` and the
ISO timestamps to `timestamptz`.  The Python layer already binds JSON strings
as `jsonb` and ISO strings as `timestamptz`, and reads both back as text, so
old (TEXT) and new (jsonb/timestamptz) schemas behave identically to the app.
