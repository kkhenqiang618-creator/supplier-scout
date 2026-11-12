# Browser network egress repair

User authorizes local repair and verification only; no push/deploy or production data changes.

Latest base 62a711f includes API test and entry recovery fixes but no browser upstream routing.

Use explicit SCOUT_BROWSER_PROXY_URL (HTTP proxy, no credentials) rather than silently inheriting machine settings or hardcoding a port. Both Moli and Chrome must enter GuardProxy; do not give page-controlled URLs an unfiltered upstream. With a configured upstream, resolve target A (AAAA fallback) using fixed Cloudflare DoH through that upstream, validate every returned address as public, then CONNECT to the validated IP, preserving browser TLS/SNI verification. Direct mode stays unchanged. No unsafe direct fallback when configured upstream fails. Redirects and subresources retain per-target checks. Robots and QR downloads use the same guarded route when enabled.

Alternatives: increasing timeouts does not fix failed TCP/TLS; sending browser straight to system proxy loses pinned-IP network protection. Guarded chaining is the chosen implementation judgment under the requested fix scope. Explicit configuration is needed because the computer proxy cannot be used on cloud workers.

Verify local socket fixtures for CONNECT/plain HTTP/denial/close/fail-closed, browser launch wiring, existing regression suite, then real public ai.centos.hk/aiberm/aixor pages through the configured route without model requests. No production sampling or claims of overall discovery improvement.
