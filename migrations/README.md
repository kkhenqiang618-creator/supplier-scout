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
ISO timestamps to `timestamptz`.  The Python layer sends JSON and ISO strings
untyped and reads rows tolerantly (dict/list as well as text, `datetime` as well
as text), so old (TEXT) and new (jsonb/timestamptz) schemas behave identically
to the app.

The JSON columns carry TEXT defaults (`'{}'` / `'[]'`) in
`cloud_schema.sql`.  Postgres cannot cast such a default to `jsonb`, so the
script drops the default before the type change and restores it as
`'{}'::jsonb` / `'[]'::jsonb` afterwards (history.offers has no default and is
left without one).  Without that, `ALTER COLUMN ... TYPE jsonb` fails with
`default for column "contacts" cannot be cast automatically to type jsonb`.

## Verifying 0002 before production

Never run it on the production database first.  Use a scratch database that was
created from `cloud_schema.sql` + `cloud_tenants.sql` (a PGlite instance is
enough; it speaks the Postgres wire protocol):

```sh
psql "$SCRATCH_DATABASE_URL" -1 -f migrations/postgres/0001_schema_migrations.sql
psql "$SCRATCH_DATABASE_URL" -1 -f migrations/postgres/0002_json_and_time_columns.sql
psql "$SCRATCH_DATABASE_URL" -1 -f migrations/postgres/0002_json_and_time_columns.sql   # second run: no change
```

Then check all three points:

1. The script exits successfully and `SELECT count(*) FROM scout_private.suppliers`
   is unchanged.
2. `\d+ scout_private.suppliers` shows `contacts/offers/sources/warnings/contact_result`
   as `jsonb` with defaults `'{}'::jsonb` / `'[]'::jsonb`, and
   `checked_at/created_at/contact_checked_at` as `timestamptz`.
3. An insert that relies on the defaults still works, e.g.
   `INSERT INTO scout_private.suppliers(name,url,mode,created_at) VALUES ('x','https://x.example.com','real',now())`
   then `SELECT contacts,offers FROM scout_private.suppliers WHERE name='x'` returns `{}` and `[]`.

After the preview passes, take a backup and run the same two files (in order,
with `-1`) on production, after deploying the code that reads both shapes.

## Worker status (0003)

`0003_worker_status.sql` adds `scout_private.cloud_worker_status`, the single row the
`browser-worker` daemon overwrites every 10 seconds. The page uses it to show whether the
后台进程 is online and where it stopped, which is the only way to tell a stuck queue apart
from a daemon that never started (Vercel's free plan keeps no history logs).

It is additive and needs no data backfill: run it once, in order after 0001 and 0002, with
`-1`. Until it runs, the rest of the app works unchanged, the worker only prints
`Worker status write failed: … 0003_worker_status.sql` once, and the page shows
后台状态未知.
