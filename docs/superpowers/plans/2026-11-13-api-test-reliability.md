# API connection test reliability

Goal: accurately show the latest test outcome and tolerate one transient connection failure without changing account credentials or provider endpoints.

Scope: ModelService test handling, its transactional Postgres adapter, settings UI, and regression tests. Keep the local testing checkout untouched. Do not claim to resolve provider routing without evidence.

1. Reproduce a successful test followed by a failure, and a temporary connection failure followed by recovery. Verify changed configurations cannot be overwritten by an older test.
2. Persist latest test status/time; invalidate tested on failure; retry only connection/upload/TLS failures once. Enforce a 65-second total network deadline covering discovery and both attempts; only the caller persists results, leaving time for database status writes. Late network responses cannot overwrite timeout failure. No extra rate-limit waits.
3. Refresh settings after failed tests, label historical success clearly, and remove premature assignment of fault to cloud egress.
4. Run Python and frontend regression checks, inspect the final diff, then publish the authorized cloud fix and verify production assets and real connection results.

Validation boundaries: successful tiny requests do not prove full browser collection reliability. No production supplier results are overwritten during verification.

Validation: 291 Python tests ran successfully (7 platform/database tests skipped); workspace12, admin and failed-test frontend regressions pass. Independent review confirmed caller deadline and late-response status fencing. Limitation: the deadline stops waiting, not an already active urllib socket; pathological continuously trickling providers may retain a daemon network thread until the connection closes.
