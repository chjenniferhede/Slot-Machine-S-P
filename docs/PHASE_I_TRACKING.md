# Phase I tracking — secure slot machine

Repository copy of [tracking issue #8](https://github.com/chjenniferhede/Slot-Machine-S-P/issues/8).
Full design: [PLAN.md](https://github.com/chjenniferhede/Slot-Machine-S-P/blob/main/docs/PLAN.md).
Requirements: [Requirement.md](https://github.com/chjenniferhede/Slot-Machine-S-P/blob/main/ProvidedResources/Requirement.md).

## Scope and status

Build a Python 3.11+ CLI slot machine with separate player, technician, and
commissioner interfaces, two mechanically distinct games, secure spin randomness,
and a tamper-evident audit log. Preserve one enforcement point for each security
property: Money, the RNG, the audit writer, and the role decorator.

`main` contains the merged identity and Layer 4–6 work (`d156463`). The
`feat/web-client` branch adds the remaining application features and the
delivery documents; it awaits review and merge. Checked items correspond to
issues already closed as completed.

- [x] [#1 — Layer 0: Foundation](https://github.com/chjenniferhede/Slot-Machine-S-P/issues/1) — scaffold, Money, SQLite schema and transactions.
- [x] [#2 — Layer 1: Integrity](https://github.com/chjenniferhede/Slot-Machine-S-P/issues/2) — hash-chained audit log and secure RNG.
- [ ] [#3 — Layer 2: Identity](https://github.com/chjenniferhede/Slot-Machine-S-P/issues/3) — merged to `main`; staff account provisioning added on `feat/web-client`. Close after review.
- [x] [#4 — Layer 3: Game domain](https://github.com/chjenniferhede/Slot-Machine-S-P/issues/4) — engine, game configurations, and payout calculator.
- [ ] [#5 — Layer 4: Money movement](https://github.com/chjenniferhede/Slot-Machine-S-P/issues/5) — merged to `main` and now driven by the player web pages. Close after review.
- [ ] [#6 — Layer 5: Interfaces](https://github.com/chjenniferhede/Slot-Machine-S-P/issues/6) — implemented on `feat/web-client` as a local web client for all three roles (see below); review pending.
- [ ] [#7 — Layer 6: Assurance](https://github.com/chjenniferhede/Slot-Machine-S-P/issues/7) — tests, scans, INSTALL.md, MANUAL.md and OWASP.md on `feat/web-client`; clean-machine install, demo rehearsal and teach-back remain (people, not code).

Keep issue #8 open until all layers and the delivery checks below are complete.

## What `feat/web-client` adds

The team chose a browser interface for all roles in place of role menus in the
terminal. The requirement allows a web front-end; the CLI remains for `--init`
and the identity menu.

- Startup installs the shipped games into a fresh database
  (`game/catalog.py`, `__main__.startup`), so a new install is playable.
- `stats.py`: read-only queries for player statistics, the technician
  diagnostic and commissioner analysis; live and test spins never mix.
- `technician.py`: enable/disable games (audited), install a game from JSON
  with an exact-RTP check (starts disabled), PAR sheets, diagnostic, audit log
  viewer and chain verification, staff account creation with one-time passwords.
- `commissioner.py`: analysis, PAR sheets, and test sessions that use the real
  RNG and engine with a virtual bankroll, show every seed, and replay every
  round afterwards. No account is touched.
- `accounts.py`: player game list, current game and own statistics.
- `web/`: a standard-library HTTP server on 127.0.0.1 (`slotmachine --web`)
  with Host/Origin/CSRF checks, HttpOnly SameSite=Strict cookies, a strict
  CSP and no tracebacks; a bilingual parody casino for players and a plain
  back office for staff.
- The role-module decorator scan now covers `technician.py` and
  `commissioner.py` (no more skipped tests). New tests drive every route over
  real HTTP and scan the static files for HTML sinks and remote loads.
- `pyproject.toml` now ships `schema.sql`, the game JSON and the web files, so
  a regular (non-editable) install works. Before this, only editable installs did.
- `tools/demo_seed.py` fills a throwaway database with play history for the demo.

## Next work

| Work | Owner | Notes |
| --- | --- | --- |
| Review and merge `feat/web-client` | Team | One PR; describe the tests that ran. |
| Close #3 and #5 | Maintainer | After the merge. |
| Clean-machine install, twice | Someone who did not write it | Follow `docs/INSTALL.md` literally on another machine. |
| Demo script and timing | Team | Under 10 minutes; use `tools/demo_seed.py` for history; rehearse the tamper check. |
| Teach-back | Team | Each person explains a layer they did not write. |

Run both `python -m pytest` and `python -m pytest -m slow`: the default run
excludes the four slow conservation/RTP checks (about 20 minutes together).
Startup recovery assumes one running application per database.

Validation of `feat/web-client` on Windows: the default suite passed
(922 tests, none skipped), the slow suite passed (4 tests, about 23 minutes),
strict mypy passed on 22 source files, a wheel built from the branch
installed into a clean virtual environment from the hash-pinned requirements,
and all three roles were walked through in a browser.

## Timeline

All dates are in November 2026.

| Date | Milestone |
| --- | --- |
| 12 | Target: player interface demoable end to end. |
| 15 | Target: submission package finished. |
| 17 | Phase I submission and Check-in 3. |
| 18 | Implementation Demo Day: 10-minute demo and oral exam, 30 minutes total. |

Reserve time before submission for installation rehearsal and team teach-back.
Every member should explain a layer they did not implement and answer questions
about arbitrary code; specializing during implementation does not divide the exam.

## Implementation decisions to carry into integration

- Use the team's settled Python 3.11+ decision; keep the rationale in PLAN.md.
- Keep Classic Sevens and Five Line Deluxe as configuration data in JSON.
- Use integer cents through Money and bind SQL values with `?` parameters.
- Derive spin stops from fresh `secrets` seeds through `rng.py`; preserve replay.
  Never import `random`. Credential generation may use `secrets` in `auth.py`,
  and payment token generation in `payment.py`, as those layers are implemented.
- Keep the current Classic Sevens hold policy from
  `src/slotmachine/game/specs/classic_sevens.json`: one consecutive re-spin,
  at most one held reel, charged at **200% of the base bet**. The original issue's
  full-bet/two-hold description predates the current implementation. Recheck
  optimal-hold return whenever the price, restrictions, strips, or paytable change.
- Keep player and commissioner rounds in the same engine; exclude test rounds
  from live money and player statistics. Show seeds only to commissioners.
- Enforce role separation and forced password changes at action boundaries,
  rather than relying on hidden menu options.
- Keep each money mutation and its business audit event in one transaction.
  An access-granted event records permission, not successful completion.
- Store mock payment tokens, last4 and required non-sensitive metadata only;
  never store a full card number or CVV. Never log passwords or hashes.
- Use the audit verification feature while documenting its tail-truncation limit.

## Delivery checklist

Items checked here are done on `feat/web-client` and still need the merge.

- [ ] All seven layer issues are implemented, reviewed, and integrated.
- [ ] Full pytest suite and configured strict mypy checks pass after integration.
- [x] Real technician and commissioner entry points carry `__required_roles__`;
      the role matrix and forced password-change gate are tested end to end.
- [x] Player flow works from registration through deposit, buy-in, spins/holds,
      cash-out, withdrawal, and own statistics.
- [x] Technician diagnostics, game management, audit reading and verification work.
- [x] Commissioner analysis and seed-visible test mode leave live balances unchanged.
- [x] Conservation, interrupt/rollback, concurrency, and RTP checks in #7 pass.
- [ ] Audit tamper demonstration is rehearsed, including the append-only trigger
      behavior and the documented limits of the chain.
- [x] `docs/INSTALL.md`, `docs/MANUAL.md`, and `docs/OWASP.md` are complete.
- [ ] Installation instructions are followed literally from a clean clone on
      another machine, once mid-build and again before submission.
- [ ] Team teach-back and timed demonstration are rehearsed.
- [ ] Submission archive excludes databases, password hashes, payment tokens,
      virtual environments, caches, and other generated development files.

## Maintaining the GitHub tracker

This is a repository documentation patch. Applying it does not edit a GitHub
issue or close any issue automatically. To synchronize issue #8, an authorized
maintainer can paste this document into its body, or run from the repository root:

```sh
gh issue edit 8 --repo chjenniferhede/Slot-Machine-S-P --body-file docs/PHASE_I_TRACKING.md
```

Refresh the snapshot and checklist when implementations are integrated. Keep the
detailed acceptance criteria in the individual layer issues.
