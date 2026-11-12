# Workspace Workflow Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans task by task; disjoint backend implementation may be delegated using dispatching-parallel-agents. Checkboxes track completion.

**Goal:** Ship the approved workspace, follow-up, pricing and log redesign without replacing production data.

**Architecture:** Preserve current app routes, authentication and RLS. A focused workspace frontend overrides page rendering; scoped settings persist follow-ups and draft recipients; a read-only pricing endpoint supplies complete offers and history.

**Tech Stack:** Python standard HTTP server, SQLite/Postgres, native JavaScript/CSS.

**Spec:** `docs/superpowers/specs/2026-11-12-workspace.md`

## Global Constraints

- Never reset or migrate production data; no external message sending.
- All follow-up/pricing/draft access is scoped to the authenticated member; no provider secrets in new responses.
- Full current offers, matching currency/unit/group/resource and known discount basis are required for distribution; unknowns stay separate.
- Existing collection, administrator view, exports, imports and credentials behavior must be preserved.
- Only validated files are committed; no real supplier data, credentials or private screenshots in Git.

## Review Focus

- Member-forged supplier IDs cannot write or read follow-ups or pricing outside their scope.
- Cloud summaries truncate offers; analytics must fetch complete quotes and expose load failures.
- Missing/ambiguous baselines, manual decisions and mixed quote currencies/units cannot silently enter distribution.
- Cancelled/failed contact saves cannot remove a supplier; reload/account changes must restore the correct follow-up state.
- Numeric names and named records need consistent IDs and direct links without dropping original data.

### Task 1: Scoped persistence and complete analytics API

Files: `server.py`, `cloud_store.py`, `tests/test_followups.py` and focused HTTP/tenant regression tests.

Interfaces: `state.followups` keyed by supplier id; `POST /api/followup {id,status,note}` returns saved record; statuses `waiting,replied,quote,done`; `GET /api/pricing?mode=real|demo` returns `{suppliers:[{id,name,url,mode,offers,checked_at}],history,rules}`. No secrets. Existing state rules remain authoritative. Add `/workspace.js` and `/workspace.css` static routes. Recipient choice, if supplied to `/api/drafts`, is validated against that supplier's current email list, persists in scoped settings and determines .eml To.

- [x] Write failing tests for follow-up persistence/reload, invalid status, missing/cross-member supplier, complete analytics beyond 3 offers and chosen recipient.
- [x] Observe failures; implement with existing transactions/settings and preserve review/contact data.
- [x] Run focused tests and report commands/results; no production credentials or database initialization.

### Task 2: Workspace pages and pricing calculations

Files: `static/app.js`, `static/workspace.js`, `static/workspace.css`, `static/index.html`, `tests/test_workspace_frontend.cjs`.

- [x] Write behavior tests for full-offer histogram, boundaries, resource/group/unit filtering, unknown discount exclusions, model union, follow-up placement and email eligibility; observe failures.
- [x] Implement shared identity links, contact/review merge, follow-up saves, dashboard charts, drafts, pricing filter/distribution/history and paged logs.
- [x] Run Node behavior tests, syntax checks and local browser checks including cancel/save/reload, pricing full-data loading, legacy admin account switching, import/export and mobile widths.

### Task 3: Verify, review, publish

- [x] Run full Python suite, Node suites, syntax checks and git diff check; fix introduced regressions.
- [x] Dispatch fresh whole-change reviewer and resolve material findings.
- [ ] Recheck remote main has not changed; commit selected files and publish atomically against the expected head, preserving remote changes.
- [ ] Wait for production deployment Ready and check authenticated/static endpoints without collecting real suppliers.
- [ ] Update project status with commit/deployment evidence and explicit remaining limitations.

## Verification evidence before publishing

Python: 142 tests, OK (4 dedicated integration skips). Node workspace: 11/11; legacy admin regression passes. Live temporary SQLite HTTP/browser: contact cancel/save/reload, follow-up history/state, draft recipients and EML, complete histogram/history, logs pagination, desktop and 320/390px overflow checks pass with no JavaScript errors. Backend rule changes invalidate cached analytics. Independent review found 3 material analytics issues; each reproduced before fix and covered by passing regressions. No production collection or schema migration performed.
