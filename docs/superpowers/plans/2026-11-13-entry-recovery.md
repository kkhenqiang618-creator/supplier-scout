# Entry recovery implementation plan

**Goal:** Recover verified domain migrations and allow safe URL correction without losing tenant data.
**Spec:** docs/superpowers/specs/2026-11-13-entry-recovery-design.md
**Execution:** inline implementation, tests first, one independent final review. Existing checkout reused, branch codex/entry-recovery.

## Global constraints
No hardcoded supplier domains in production code. Preserve contacts, quotes, decisions, drafts and identity. No private data/credentials in git. No schema migration. Public-only targets and existing robots protections. At most2 migration hops per browser run; no model step for deterministic recovery. User author/closed-site contact policy unchanged.

## Review focus
- Migration notices must not turn generic docs/ads into verified supplier URLs.
- Reject private/protocol downgrade/ambiguous and cyclic targets; reject error/challenge/empty destinations.
- Manual/automatic URL changes preserve history and reject duplicates, stale requests and active-job races.
- Tenant boundary covers new write endpoint and details/export.
- Recovery failures retain observations/partial contacts and still allow Chrome when appropriate.

### Task1: Migration observations and navigation
- [ ] Write RED unit/real browser tests for explicit migration, ambiguity, meta refresh, unsafe targets, readable destination, failed destination and bounded chain.
- [ ] Implement entry_recovery.py, observe meta refresh in agent_browser.cjs, integrate browser_agent.py and retain partial evidence, then GREEN.

### Task2: Safe entry update and persistence
- [ ] Write RED API/Store tests for preservation, history survives contacts save, duplicates/stale/private/active job rejection and automatic recovery persistence.
- [ ] Implement Store/Postgres write method and authenticated /api/supplier-entry, cloud summary metadata; GREEN.

### Task3: UI and release
- [ ] Add update URL modal and recovery history rendering; verify runtime and escaping.
- [ ] Full suites and local Moli/Chrome migration fixture; one independent review and fix important findings.
- [ ] Commit, recheck remote head, push, verify Function/Daemon ready, preserve production snapshot then update id18 and attempt one-site contacts acceptance. Record actual outcomes in PROJECT_STATUS.md.
