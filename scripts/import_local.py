"""One-time private local SQLite import. Credentials are read only from env/files."""
import argparse,contextlib,json,sqlite3,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from cloud_store import PostgresStore

LEGACY_OWNER='小姜'  # The old app was single-user: all rows in the source database belong to this account.

def migrate(source,provider=None):
    # Import as the legacy owner: rows are written under an explicit user context, so the
    # owner default and the RLS policy both resolve to this account instead of guessing.
    store=PostgresStore().for_user(LEGACY_OWNER);counts={}
    with contextlib.closing(sqlite3.connect(source)) as source_db,store.database.transaction(write=True) as db:
        source_db.row_factory=sqlite3.Row
        if db.execute('SELECT 1 FROM suppliers LIMIT 1').fetchone():raise ValueError('云端已有供应商，停止导入以避免覆盖；请人工检查迁移状态')
        for table in ['suppliers','settings','events','history','drafts']:
            rows=source_db.execute('SELECT * FROM '+table).fetchall();counts[table]=len(rows)
            for row in rows:
                values=dict(row);values.setdefault('owner',LEGACY_OWNER);columns=list(values)
                sql='INSERT INTO '+table+'('+','.join(columns)+') VALUES('+','.join('?' for _ in columns)+') ON CONFLICT('+('key' if table=='settings' else 'id')+') DO UPDATE SET '+','.join(name+'=excluded.'+name for name in columns)
                db.execute(sql,tuple(values.values()))
            if table!='settings':
                maximum=db.execute('SELECT max(id) AS maximum FROM '+table).fetchone()['maximum']
                db.execute("SELECT setval(pg_get_serial_sequence(?, 'id'),?,?)",('scout_private.'+table,maximum or 1,maximum is not None))
        if provider:store.model_service._write(json.loads(Path(provider).read_text()))
    return counts

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('source');parser.add_argument('--provider');args=parser.parse_args();print(json.dumps(migrate(args.source,args.provider),ensure_ascii=False))
