---
name: sqlite-integrity
description: Preserve SQLite and audit consistency in this slot machine. Use for storage, schema, authentication state, registration, audit events, balances, or transactions.
---

# SQLite integrity

1. Read `storage.py`, `schema.sql`, and `audit.py` before changing persistence.
   Use `storage.connect` to retain foreign keys and explicit transaction control.
2. Bind user values with `?` parameters. Keep SQL identifiers static and inspectable.
3. Store amounts as `Money` integer cents and respect signed 64-bit limits. Never
   create a wallet for staff; keep commissioner test spins out of live accounting.
4. Use `storage.transaction` (`BEGIN IMMEDIATE`) for state changes and their audit
   entries. Preserve rollback on `BaseException`, including Ctrl-C.
5. Check transaction ownership explicitly. Avoid nested BEGINs. If a helper joins
   a caller's transaction, document that the caller owns commit/rollback. Commit
   login failure counters and denials before raising their expected errors when
   the helper owns the transaction.
6. Serialize bootstrap's empty-database check with user creation. Check lockout
   and update counters under the same write lock to avoid lost attempts.
7. Keep audit events structured and free of secrets. Preserve the append-only
   triggers and hash-chain encoding; explain the tail-truncation limitation.
8. Test rollback, independent database connections, persistence across reconnects,
   and intact audit chains after successful and failed operations.
