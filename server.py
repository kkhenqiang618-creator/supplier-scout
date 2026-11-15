#!/usr/bin/env python3
"""Supplier Scout local workspace entry point.

The implementation lives in three modules: collection.py (site collection
subprocess protocol and shared helpers), local_store.py (SQLite store and the
local batch target) and web_app.py (HTTP layer).  This module keeps the historic
`server.<name>` import surface and owns the CLI entry point, so tests and
scripts keep importing the same names while cloud modules no longer import the
HTTP entry point to reach the store.
"""

import argparse
from pathlib import Path
from browser_agent import AgentServiceError, ModelTransientSiteError
from collection import (
    ROOT,
    now,
    encode,
    log_exception,
    CollectionCancelled,
    BatchRuntimeError,
    SITE_HARD_TIMEOUT,
    _terminate_tree,
    collect_site,
    QUOTE_CHANGED_WARNING,
    manual_decision,
    merge_sources,
    merge_contacts,
    quote_fingerprint,
)
from engine import DEFAULT_RULES, evaluate, crawl, extract_page, validate_rules
from importers import MAX_FILE_BYTES, parse_file, parse_text, prepare_rows
from local_store import Store, LocalBatchTarget, draft_content, make_draft
from web_app import Handler, create_server

# Re-exported for tests and scripts written against the pre-split module.
__all__ = [
    'ROOT',
    'now',
    'encode',
    'log_exception',
    'CollectionCancelled',
    'BatchRuntimeError',
    'SITE_HARD_TIMEOUT',
    '_terminate_tree',
    'collect_site',
    'QUOTE_CHANGED_WARNING',
    'manual_decision',
    'merge_sources',
    'merge_contacts',
    'quote_fingerprint',
    'AgentServiceError',
    'ModelTransientSiteError',
    'DEFAULT_RULES',
    'evaluate',
    'crawl',
    'extract_page',
    'validate_rules',
    'MAX_FILE_BYTES',
    'parse_file',
    'parse_text',
    'prepare_rows',
    'Store',
    'LocalBatchTarget',
    'draft_content',
    'make_draft',
    'Handler',
    'create_server',
    'main',
]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--db')
    args = parser.parse_args()
    (ROOT / 'data').mkdir(exist_ok=True)
    database_path = Path(args.db) if args.db else ROOT / 'data' / 'supplier.db'
    new_workspace = not database_path.exists()
    app = create_server(args.port, args.db)
    if new_workspace:
        from rulebook import A_RULES

        app.store.update_rules(A_RULES)
    print('Supplier Scout → http://127.0.0.1:' + str(app.server_port), flush=True)
    try:
        app.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.store.stop_job()
        if getattr(app.store, 'job_thread', None):
            app.store.job_thread.join(timeout=3)
        app.server_close()


if __name__ == '__main__':
    main()
