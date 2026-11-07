# Project agent guide

Read `docs/PLAN.md`, the selected GitHub issue, and the affected modules before
editing. Use Python 3.11+, the existing src layout, and the standard library unless
the pinned dependencies already provide the capability. Check issue prerequisites
against current code. Keep changes on a feature branch and open a reviewable PR.

## Project skills

- Read `.agents/skills/secure-python/SKILL.md` for authentication, roles, CLI input,
  secrets, randomness, or game logic changes.
- Read `.agents/skills/sqlite-integrity/SKILL.md` for database, audit, accounting,
  registration, or login state changes.
- Read `.agents/skills/security-verification/SKILL.md` when implementing or
  reviewing any issue and before reporting completion.

These are repository skills; keep them in version control alongside the code.
Do not install project guidance into a contributor's personal skills directory.

## Setup and checks

```sh
python3 -m venv .venv
.venv/bin/python -m pip install --require-hashes -r requirements.txt -r requirements-dev.txt
.venv/bin/python -m pip install --no-deps -e .
.venv/bin/python -m pytest
.venv/bin/python -m mypy
```

On Windows use `.venv\Scripts\python` in place of `.venv/bin/python`.
Keep passwords, payment data, local databases, and virtual environments out of Git.
Describe actual tests and remaining integration work in the PR. Do not mark a
layer complete when its acceptance criteria depend on unimplemented interfaces.
