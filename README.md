# Big Casino Secure Slot Machine — Phase I

A secure, auditable command-line slot machine for the Security & Privacy in Computing
course project (Fall 2026), Phase I "The Engineer".

| Document | What it covers |
|---|---|
| [`docs/INSTALL.md`](docs/INSTALL.md) | **Installation instructions.** Start here. |
| [`docs/MANUAL.md`](docs/MANUAL.md) | **User manual** for players, technicians and commissioners. |
| [`docs/OWASP.md`](docs/OWASP.md) | Security design: every OWASP Top 10 (2021) category, its controls and CWEs. |
| [`docs/PLAN.md`](docs/PLAN.md) | Original design and architecture. |

## Quick start

Requires **Python 3.11 or newer** (see INSTALL.md if your system Python is older).

```sh
python3 -m venv .venv && source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install --require-hashes -r requirements.txt
python -m pip install --no-deps -e .
slotmachine --init      # save the tech-admin password it prints
slotmachine             # terminal menus
slotmachine --web       # or the same casino in a browser, on 127.0.0.1:8888
```

## What it does

- **Players** register, link a card, deposit and withdraw, buy in to a game, spin, hold
  reels, cash out, and view their statistics.
- **Technicians** add (install, enable) and remove (disable) games, run a system
  diagnostic, read and verify the tamper-evident audit log, and create staff accounts.
- **Commissioners** analyse game statistics, test any game for X rounds with every seed
  recorded, replay any spin from its seed, and read each game's PAR sheet.
- Two games: **Classic Sevens** (3 reels, holds) and **Five Line Deluxe** (5×3, five
  paylines including V-shaped diagonals), each with three bet sizes.

## Language

The specification names Go. Per the instructor's guidance for this team, the system is
written in Python 3.11+; the rationale is recorded in `docs/PLAN.md`.

## Layout

```
src/slotmachine/
  __main__.py        entry point: database setup, login, role dispatch
  auth.py            Argon2id passwords, lockout, @require_role, staff accounts
  accounts.py        balance, deposits/withdrawals, buy-in, spins, holds, cash-out
  payment.py         card validation and mock tokenisation (no card numbers stored)
  stats.py           player, game and distribution statistics (read-only)
  rng.py             the only randomness: secrets seed -> unbiased reel stops
  audit.py           SHA-256 hash-chained audit log and verification
  money.py           integer-cent Money type
  storage.py         SQLite access, transactions; schema.sql (tables, triggers, views)
  game/              spec.py (JSON game definitions), engine.py (pure payout logic), specs/*.json
  cli/               prompts.py, menus.py, common.py, player.py, technician.py, commissioner.py
  tools/rtpcalc.py   exact RTP and PAR sheet calculator
tests/               pytest suite (about 770 tests + 4 slow soak tests)
```
