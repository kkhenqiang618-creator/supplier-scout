# Contact reliability implementation plan

> Execution: implement inline, then one independent whole-branch review, as authorized by the user's request to update, push and deploy.

**Goal:** Reduce wasted contact-finding steps and expose actionable failure reasons without narrowing what counts as a contact.
**Architecture:** Browser readiness and safe actions in agent_browser.cjs; bounded navigation and diagnostics in browser_agent.py; existing contact_result JSON persistence and native HTML rendering.
**Spec:** docs/superpowers/specs/2026-11-12-contact-reliability-design.md

## Global Constraints
Preserve data and tenant boundaries. No credentials in git. No schema migration. Keep author/third-party/closed-site contacts found. Keep existing per-command/site hard limits and SSRF guards.

## Review Focus
- A default submit button outside a form is harmless, but a true form submit or destructive link must remain blocked.
- Long pages with delayed contact modals must settle even when their text exceeds the weak-render threshold.
- Infinite spinners/challenges must terminate without repeated model decisions or exceeding command deadlines.
- Action refs can change, while distinct controls with similar labels must remain usable.
- Legacy and current failure diagnostics must reach summary and detail APIs without leaking other accounts.

### Task 1: Browser actions and readiness
- [x] Add real-browser fixture tests for notification/default submit, true submit blocked, delayed long page/modal, HTTP404 and bounded loading.
- [x] Run tests RED; implement safe navigation and bounded fingerprint settling in agent_browser.cjs; run GREEN.

### Task 2: Agent control and diagnostics
- [x] Add tests for repeated unchanged actions, loading/challenge auto waits, history navigation, classification, contact status preservation and persistence.
- [x] Run RED; implement bounded controls and structured diagnostics, then GREEN.

### Task 3: UI and release
- [x] Add UI tests for old/new reason display and escaping; implement summary/list/detail display.
- [x] Run Python/Node suites and real Moli/Chrome fixtures, independent review and fix important findings.
- [ ] Commit verified source only; recheck remote head; push/deploy; verify production binding and worker image status; record actual evidence in PROJECT_STATUS.md.
