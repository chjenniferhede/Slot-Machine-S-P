# Phase I tracking — secure slot machine

Repository copy of [tracking issue #8](https://github.com/chjenniferhede/Slot-Machine-S-P/issues/8).
Full design: [PLAN.md](https://github.com/chjenniferhede/Slot-Machine-S-P/blob/main/docs/PLAN.md).
Requirements: [Requirement.md](https://github.com/chjenniferhede/Slot-Machine-S-P/blob/main/ProvidedResources/Requirement.md).

## Scope and status

Build a Python 3.11+ CLI slot machine with separate player, technician, and
commissioner interfaces, two mechanically distinct games, secure spin randomness,
and a tamper-evident audit log. Preserve one enforcement point for each security
property: Money, the RNG, the audit writer, and the role decorator.

This snapshot reflects upstream `main` at
`6ecbeffc51c3ab1253ee5e080c7be71336bf0fb9` and the linked issues at patch preparation.
Checked items below correspond to issues closed as completed. An implementation
available only as an unapplied patch is still pending integration.

- [x] [#1 — Layer 0: Foundation](https://github.com/chjenniferhede/Slot-Machine-S-P/issues/1) — scaffold, Money, SQLite schema and transactions.
- [x] [#2 — Layer 1: Integrity](https://github.com/chjenniferhede/Slot-Machine-S-P/issues/2) — hash-chained audit log and secure RNG.
- [ ] [#3 — Layer 2: Identity](https://github.com/chjenniferhede/Slot-Machine-S-P/issues/3) — separate tested patch prepared; application, review, and integration pending.
- [x] [#4 — Layer 3: Game domain](https://github.com/chjenniferhede/Slot-Machine-S-P/issues/4) — engine, game configurations, and payout calculator.
- [ ] [#5 — Layer 4: Money movement](https://github.com/chjenniferhede/Slot-Machine-S-P/issues/5) — payment methods, deposits, withdrawals, buy-in and cash-out.
- [ ] [#6 — Layer 5: Interfaces](https://github.com/chjenniferhede/Slot-Machine-S-P/issues/6) — player, technician, and commissioner workflows.
- [ ] [#7 — Layer 6: Assurance](https://github.com/chjenniferhede/Slot-Machine-S-P/issues/7) — verification, installation guide, manual, OWASP write-up and demo.

Keep issue #8 open until all layers and the delivery checks below are complete.
Closing a layer issue does not by itself establish that the integrated application
has passed its end-to-end checks.

## Next work and dependencies

| Work | Dependencies | Next step |
| --- | --- | --- |
| Identity (#3) | #1 and #2 complete | Apply and review the separately delivered Layer 2 patch. Re-run its tests in the destination checkout. |
| Money movement (#5) | Storage, audit, authenticated identities | Implement atomic movements and mock payments; integrate with #3 before accepting real workflows. |
| Interfaces (#6) | Framework can start now; full flows need #3, #4 and #5 | Build role menus, then wire actual privileged actions through the role decorator. |
| Assurance (#7) | Tests and docs can start now; final validation needs all layers | Add source invariants and document features as they land; finish integration and soak tests after the workflows exist. |

Layer 3 is independent of Layer 2. Layers 0 and 1 no longer block identity work.
Layer 5 remains the integration point for the first complete player demonstration.
The identity patch includes repository agent skills and basic identity menus;
those menus do not complete the Layer 5 game, payment, or staff interfaces.

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

- [ ] All seven layer issues are implemented, reviewed, and integrated.
- [ ] Full pytest suite and configured strict mypy checks pass after integration.
- [ ] Real technician and commissioner entry points carry `__required_roles__`;
      the role matrix and forced password-change gate are tested end to end.
- [ ] Player flow works from registration through deposit, buy-in, spins/holds,
      cash-out, withdrawal, and own statistics.
- [ ] Technician diagnostics, game management, audit reading and verification work.
- [ ] Commissioner analysis and seed-visible test mode leave live balances unchanged.
- [ ] Conservation, interrupt/rollback, concurrency, and RTP checks in #7 pass.
- [ ] Audit tamper demonstration is rehearsed, including the append-only trigger
      behavior and the documented limits of the chain.
- [ ] `docs/INSTALL.md`, `docs/MANUAL.md`, and `docs/OWASP.md` are complete.
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
