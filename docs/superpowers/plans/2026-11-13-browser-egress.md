# Browser Egress Implementation Plan

**Goal:** repair browser egress without deploying.
**Spec:** ../specs/2026-11-13-browser-egress.md
**Architecture:** explicit upstream, public-IP validated remote DNS, GuardProxy chaining; unchanged direct behavior.
**Tech Stack:** Python stdlib; existing Moli and Playwright.

## Constraints and review focus
No global network settings, credentials in output, production writes, push or deployment. Reject private/mixed DNS, invalid configuration, redirect bypass, failed proxy, credentials leakage. Keep sockets bounded and close owned proxies.

1. [x] Add failing upstream tests: config, public remote DNS, CONNECT pinned IP, proxy error, private block, plain HTTP; run RED. Implement browser_egress.py and GuardProxy upstream transport; run GREEN.
2. [x] Add failing browser integration tests: Moli guarded proxy launch and close, configured robots/image routing; run RED. Wire both browsers and auxiliary fetches; run GREEN.
3. [x] Run full Python/Node regressions and public browser sample; review diff and record limitations. Document configuration; commit only isolated repair, no push/deploy.

Validation: 323 Python cases, 316 passed / 7 skipped; 11 egress-specific cases; Node workspace 13 cases plus API-status/admin/QR checks passed; Chrome readiness 7 cases passed. Actual proxy Moli/Chrome ai.centos.hk text 8017/4394 and HTTP200; Moli aiberm text1701. Independent review reproduced NO_PROXY=* guard rejection against owned loopback fixture (zero hits). aixor Moli HTTP200 but empty body; Chrome comparison recorded outside repo. No full model collection, cloud daemon or production data validation. No push/deploy.
