---
name: security-verification
description: Verify slot machine issue acceptance criteria and security boundaries. Use when implementing or reviewing an issue and before delivering project changes.
---

# Security verification

1. Map the selected issue's acceptance criteria to code and observable tests.
   Distinguish implemented behavior from work assigned to later layers.
2. Install from the hash-pinned requirements, then install the package without
   changing dependency versions. Run focused tests while editing, then the full
   pytest suite and configured strict mypy checks.
3. Exercise every role against an independently written expected permission
   matrix, plus anonymous access. Test forced password change at the API boundary
   as well as through the CLI. Check failure messages and dummy hash verification
   by mocking calls rather than comparing elapsed times.
4. Test atomicity by injecting failures into audit writes and action bodies.
   Confirm failed login counters survive expected errors and reconnects; confirm
   an interrupted money change leaves neither partial balances nor orphan rows.
5. Scan changed source for insecure RNG, unsafe deserialization, interpolated SQL,
   secret logging, hardcoded credentials, and missing privilege decorators.
6. Use temporary databases and mocked hidden prompts in tests. Never commit a
   bootstrap password or a populated database. Manually smoke-test installation,
   console scripts, first-run bootstrap, login, and password changes.
7. Review `git diff --check` and the final diff. Report commands actually run,
   results, and any unimplemented acceptance criteria in the PR.
