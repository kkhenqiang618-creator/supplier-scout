"""One-time copy of one account's cloud records into the local SQLite workspace (P28).

The cloud database is only read: every statement the copy issues runs inside a
non-writing transaction scoped to the requested account, so the ownership rules
and row-level security decide what is visible instead of being bypassed (business
rule 1). Model configuration, API keys and login sessions are never copied, and
the local database is backed up before anything is written. An existing workspace
that already holds real suppliers is left untouched.
"""

import argparse
import contextlib
import datetime
import os
import shutil
import sqlite3
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from collection import encode, load_json  # noqa: E402
from engine import validate_rules  # noqa: E402
from local_store import Store  # noqa: E402

TABLES = ('suppliers', 'events', 'history', 'drafts')
JSON_COLUMNS = {'contacts', 'offers', 'sources', 'warnings', 'contact_result'}
TIME_COLUMNS = {'checked_at', 'contact_checked_at', 'created_at', 'archived_at'}
FOLLOWUP_PREFIX = 'followups:'
DEFAULT_LOCAL = ROOT / 'data' / 'supplier.db'


def _text(column, value):
    """One cell as SQLite text: jsonb/TEXT documents and timestamptz/TEXT times both end up as text."""
    if value is None:
        return None
    if column in JSON_COLUMNS:
        return encode(load_json(value, {} if column in ('contacts', 'contact_result') else []))
    if isinstance(value, datetime.datetime):
        return value.isoformat(timespec='seconds')
    return str(value)


PAGE_SIZE = 20
WORKERS = 6


def _ids(db, table):
    return [row['id'] for row in db.execute('SELECT id FROM ' + table + ' ORDER BY id')]


def _fetch(db, table, ids):
    """Read the given ids on this connection, in small messages.

    Supplier rows carry the collected page snapshots (hundreds of KB each). A single
    large result makes the managed pooler stall, and one connection only gets a few
    KB per second, so the ids are split across a handful of parallel readers.
    """
    rows = []
    for start in range(0, len(ids), PAGE_SIZE):
        chunk = ids[start:start + PAGE_SIZE]
        batch = [
            dict(row)
            for row in db.execute(
                'SELECT * FROM ' + table + ' WHERE id = ANY(%s) ORDER BY id', (chunk,)
            )
        ]
        rows.extend(batch)
    return rows


def _read_table(db, table, workers=WORKERS):
    with db.transaction(write=False) as conn:
        ids = _ids(conn, table)
    if not ids:
        return []
    if len(ids) < PAGE_SIZE or workers <= 1:
        with db.transaction(write=False) as conn:
            return _fetch(conn, table, ids)
    buckets = [ids[index::workers] for index in range(workers)]
    results = [None] * workers

    def work(index):
        with db.transaction(write=False) as conn:
            results[index] = _fetch(conn, table, buckets[index])

    threads = [threading.Thread(target=work, args=(index,)) for index in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    rows = [row for bucket in results if bucket for row in bucket]
    rows.sort(key=lambda row: row['id'])
    return rows


def read_cloud(store):
    """Everything the local workspace needs, read-only and scoped to the store's account."""
    rows = {}
    for table in TABLES:
        rows[table] = _read_table(store.database, table)
    rows['followups'] = store.followups()
    rows['rules'] = store.rules()
    return rows


def _columns(db, table):
    return [row[1] for row in db.execute('PRAGMA table_info(' + table + ')')]


def _insert(db, table, rows, remap=None):
    """Insert rows and return their new ids, keyed by the cloud id.

    Supplier ids are renumbered because this workspace may already hold other rows
    (the demo workspace, for example). Children are remapped through the same map so
    events, history, drafts and follow-ups stay attached to the right supplier.
    """
    columns = _columns(db, table)
    ids = {}
    inserted = 0
    for row in rows:
        values = {name: _text(name, row[name]) for name in columns if name in row and name != 'id'}
        if remap is not None and 'supplier_id' in columns:
            mapped = remap.get(row.get('supplier_id'))
            if mapped is None:
                continue
            values['supplier_id'] = mapped
        names = list(values)
        cursor = db.execute(
            'INSERT INTO ' + table + '(' + ','.join(names) + ') VALUES(' + ','.join('?' for _ in names) + ')',
            tuple(values[name] for name in names),
        )
        ids[row.get('id')] = cursor.lastrowid
        inserted += 1
    return inserted, ids


def _real_rows(path):
    if not path.exists():
        return 0
    with contextlib.closing(sqlite3.connect(path)) as db:
        found = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='suppliers'").fetchone()
        if not found:
            return 0
        return db.execute("SELECT count(*) FROM suppliers WHERE mode != 'demo'").fetchone()[0]


def _backup(path):
    if not path.exists():
        return None
    target = path.with_name(path.name + '.bak-' + time.strftime('%Y%m%d-%H%M%S'))
    shutil.copy2(path, target)
    return target


def copy_cloud_to_local(owner, local_path=None, store=None):
    """Copy one account's records; returns (counts, backup path)."""
    if not isinstance(owner, str) or not owner.strip():
        raise SystemExit('请用 --owner 指定要复制的账号名')
    owner = owner.strip()
    path = Path(local_path or DEFAULT_LOCAL)
    existing = _real_rows(path)
    if existing:
        raise SystemExit(
            '本机已有 %d 条真实供应商，停止复制以免覆盖；请先备份并移走 %s 再重试' % (existing, path)
        )
    if store is None:
        if not os.environ.get('DATABASE_URL'):
            raise SystemExit('请先设置 DATABASE_URL 环境变量（只从环境读取，不写进文件或日志）')
        from cloud_store import PostgresStore

        store = PostgresStore(bootstrap=False, model_service=False).for_user(owner)
    print('正在读取云端数据（页面快照较大，可能需要几分钟）…', flush=True)
    rows = read_cloud(store)
    backup = _backup(path)
    local = Store(path, seed=False)
    counts = {}
    with local.lock, local.connect(write=True) as db:
        counts['suppliers'], supplier_ids = _insert(db, 'suppliers', rows['suppliers'])
        for table in ('events', 'history', 'drafts'):
            counts[table], _ = _insert(db, table, rows[table], remap=supplier_ids)
        followups = rows['followups'] or {}
        kept = 0
        for sid, record in followups.items():
            mapped = supplier_ids.get(int(sid)) if str(sid).isdigit() else None
            if mapped is None:
                continue
            db.execute(
                'INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)',
                (FOLLOWUP_PREFIX + str(mapped), encode(record)),
            )
            kept += 1
        counts['followups'] = kept
        rules = rows['rules']
        validate_rules(rules)
        db.execute('INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)', ('rules', encode(rules)))
        counts['rules'] = 1
    return counts, backup


def main(argv=None):
    parser = argparse.ArgumentParser(description='把云端某个账号的记录只读复制到本机 SQLite（P28）')
    parser.add_argument('--owner', required=True, help='要复制的云端账号名')
    parser.add_argument('--local', default=str(DEFAULT_LOCAL), help='本机 SQLite 路径（默认 data/supplier.db）')
    args = parser.parse_args(argv)
    counts, backup = copy_cloud_to_local(args.owner, args.local)
    if backup:
        print('已备份本机库：' + backup.name)
    names = {'suppliers': '供应商', 'events': '事件', 'history': '历史', 'drafts': '草稿', 'followups': '跟进记录', 'rules': '规则'}
    for key in ('suppliers', 'events', 'history', 'drafts', 'followups', 'rules'):
        print('%s：%d 条' % (names[key], counts[key]))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
