# Secure Slot Machine — Phase I Implementation Plan (Python)

> **On the language.** The written spec names Golang (PDF §5.2 and §3.1.1 req. 4). We are
> building in Python per the instructor's verbal guidance that teams assigned Go use Python
> instead. This is a deliberate team decision, settled — noted here only so the rationale is on
> record if it comes up at the demo.

## Context

This is the Phase I ("The Engineer") deliverable for *America's Next Top Threat Model*, the
Security & Privacy in Computing course project (Fall 2026). Our team was assigned the **Slot
Machine** system (PDF §5.2, mirrored verbatim in `ProvidedResources/Requirement.md`).

We must build an executable software package that Big Casino could plausibly deploy: three
user groups (Technicians, Commissioners, Players), at least two mechanically distinct slot
games, cryptographically secure randomness, auditable logging, and defences against every
applicable OWASP Top 10 category. Phase I is due **Tuesday, November 17**, with a live
30-minute demo and oral exam on **Wednesday, November 18**, where every team member must be
able to explain any line of the code.

The stakes beyond the grade: in Phase II another team receives this package and tries to
backdoor it. Every shortcut we take now becomes their foothold. The design below therefore
prefers **one enforcement point per security property** over convenience — one RNG function,
one access-control decorator, one money type, one audit writer.

The repo is currently empty (a one-line README plus the provided resources), so this is
greenfield.

## Decisions already made

| Area | Decision |
|---|---|
| Language | **Python 3.11+** (deviation from spec — see warning above) |
| Interface | CLI only (the PDF suggests it; keeps effort on the security story) |
| Games | Game A: 3-reel, 1 payline, **holds**. Game B: 3×5 grid, **5 paylines**, no holds |
| RNG | One path everywhere: `secrets` seed → HMAC-derived reel stops; seed always logged, **displayed only in Commissioner test mode** |
| Payout math | Explicit reel strips + exact analytic RTP, tuned to ~92–95% |
| Storage | SQLite via the stdlib `sqlite3` module |
| Payments | Mock processor; store **token + last4 + brand only**, never the PAN or CVV |
| Staff auth | Technician seeded on first run with a `secrets` password, forced change at first login |
| Hold rules | Full bet per re-spin, max 2 consecutive holds |
| Wallet | Session credits with explicit buy-in and cash-out |
| Game mgmt | Technicians enable/disable games (configs still stored as data, not hardcoded) |
| Extra feature | **Tamper-evident hash-chained audit log** + `audit verify` command |
| Testing | `pytest`, parametrised, on money-critical and security-critical code |
| Naming | Generic placeholders — `slotmachine`, "Classic Sevens", "Five Line Deluxe" |

## Delivery: GitHub issues, organised by layer

The system is a dependency stack, not a to-do list. Each layer only depends on the ones
beneath it, which is what makes it safe to hand a whole layer to one person. The output of
this plan is **one tracking issue plus seven layer issues** in
`chjenniferhede/Slot-Machine-S-P`.

```
  L6  ASSURANCE        tests · docs · demo rehearsal · OWASP write-up
      ─────────────────────────────────────────────────────────────
  L5  INTERFACES       player CLI  ·  technician CLI  ·  commissioner CLI
      ─────────────────────────────────────────────────────────────
  L4  MONEY MOVEMENT   payment methods · deposits/withdrawals · credits & cash-out
      ─────────────────────────────────────────────────────────────
  L3  GAME DOMAIN      engine · game specs · RTP calculator      ⟵ independent of L2
      ─────────────────────────────────────────────────────────────
  L2  IDENTITY         argon2id · roles · @require_role · lockout · bootstrap
      ─────────────────────────────────────────────────────────────
  L1  INTEGRITY        hash-chained audit log  ·  secure RNG
      ─────────────────────────────────────────────────────────────
  L0  FOUNDATION       scaffold · money.Cents · SQLite schema & transactions
```

| Layer | Issue title | Contains | Depends on |
|---|---|---|---|
| L0 | Layer 0: Foundation — project scaffold, money, and storage | `pyproject.toml`, `requirements.txt`, `money.py`, `storage.py`, `schema.sql`, test harness | — |
| L1 | Layer 1: Integrity — audit chain and secure RNG | `audit.py` (hash chain + `verify_chain`), `rng.py` (`secure_seed`, `derive_stops`, rejection sampling) | L0 |
| L2 | Layer 2: Identity — authentication, roles, and access control | `auth.py` (argon2id, lockout, `@require_role`), first-run bootstrap in `__main__.py` | L0, L1 |
| L3 | Layer 3: Game domain — engine, specs, and payout math | `game/spec.py`, `game/engine.py`, `game/specs/*.json`, `tools/rtpcalc.py` | L0, L1 |
| L4 | Layer 4: Money movement — payments, accounts, and credits | `payment.py`, account repo, deposit/withdraw limits, session buy-in and cash-out | L0, L1, L2 |
| L5 | Layer 5: Interfaces — the three role CLIs | `cli/player.py`, `cli/technician.py`, `cli/commissioner.py`, `cli/prompts.py`, `stats.py` | L2, L3, L4 |
| L6 | Layer 6: Assurance — verification, documentation, and demo | full test suite, source scans, soak test, install guide, user manual, OWASP write-up | all |

Plus **Issue 0: Phase I tracking** — a checklist linking all seven, with the timeline and the
parallelism notes. No assignees; the team claims layers themselves.

**Where the parallelism is.** L1 and L3 are independent of each other, and L3 is independent of
L2 entirely — the game math needs no notion of a user. So once L0 lands (roughly a day), three
people can work simultaneously: one on L1→L2, one on L3, one starting L4's payment module
against stubs. A fourth can take the CLI framework (menu loop, input validation, table
rendering) from day one, since it builds against stubs too.

L5 is the integration point and the first demoable state; it should not slip past **Nov 12**,
leaving five days for L6 and rehearsal.

**Two scheduling facts to build around:**
- **Nov 17 is double-booked** — Phase I submission *and* Check-in 3, which is about backdooring
  another team's system. Don't plan to be writing code on the 16th.
- **The oral exam is not divisible.** PDF §3.1.2: "all group members are expected to answer
  every question." Whatever the split, hold a teach-back session before Nov 18 where each
  person explains a layer they did *not* write. An hour, and it protects marks the code has
  already earned.

**Issue structure.** Each layer issue has one section per component, each with its own task
checkboxes, so sub-work is still individually assignable by checking off boxes or splitting a
section into a follow-up issue later. Each is self-contained (300–700 lines) — background,
file paths, signatures, worked examples, security notes with OWASP/CWE mappings, required
tests, acceptance criteria, and demo-day questions — so whoever picks it up does not need to
read this plan first.

No labels, no milestones, no assignees — just the eight issues. Bodies written to the
scratchpad as markdown and created with `gh issue create --body-file`. The layer name is in
the title, which is enough to sort by.

**Permanent copy of this plan:** written to `docs/PLAN.md`, left **uncommitted** for review.
It is the source the issues point back to; `docs/` will also hold `INSTALL.md`, `MANUAL.md`
and `OWASP.md` for the submission.

Repo note: `Slot-Machine-S-P` is currently **public**. Consider making it private until after
Phase II so rival teams cannot study the implementation in advance.

## Security controls at a glance

Each control is a few lines of code, applied in exactly one place. This table doubles as the
answer to check-in 2 question 11 and to the demo-day question "point to the line that defends
this category."

| OWASP | Risk | Control | Where |
|---|---|---|---|
| A01 Broken Access Control | A player reaches technician menus | `@require_role` on every privileged action, single choke point | `auth.py` |
| A02 Cryptographic Failures | Predictable spins; stolen passwords; stored card numbers | `secrets.token_bytes` only; argon2id + per-user salt; store token + last4, never the PAN or CVV | `rng.py`, `auth.py`, `payment.py` |
| A03 Injection | SQL injection via usernames or game names | `?` bound parameters everywhere; a test scans `src/` for f-string SQL | `storage.py` |
| A04 Insecure Design | Player profits via holds; money lost mid-crash | Optimal-hold RTP proven ≤ 100%; bet/spin/payout in one `BEGIN IMMEDIATE` transaction | `tools/rtpcalc.py`, `storage.py` |
| A05 Security Misconfiguration | Shipped default credentials; FKs silently off | First-run `secrets` password, forced change; `PRAGMA foreign_keys=ON` | `__main__.py`, `schema.sql` |
| A06 Vulnerable Components | Unpinned or compromised dependency | One runtime dependency, `requirements.txt --require-hashes` | `requirements.txt` |
| A07 Identification & Auth Failures | Password brute force; timing oracle on compares | 5-failure / 15-minute lockout; `hmac.compare_digest` | `auth.py` |
| A08 Data Integrity Failures | Config deserialisation RCE; silent log edits | JSON only, never `pickle` (test-enforced); hash-chained audit log + `verify` | `game/spec.py`, `audit.py` |
| A09 Logging & Monitoring Failures | Attacker activity leaves no trace | Every security-relevant action logged inside the authorisation decorator | `audit.py` |
| A10 SSRF | — | No outbound network requests exist; rationale documented, as the PDF requires | n/a |

Two defects that are not OWASP categories but are the likeliest way to actually lose money:
**float arithmetic on balances** (banned, lint-checked) and **modulo bias in reel selection**
(rejection sampling instead).

## Architecture

```
pyproject.toml              console_scripts: slotmachine, slotmachine-rtpcalc
requirements.txt            pinned with hashes (OWASP A06)
src/slotmachine/
  __main__.py               entry point, first-run bootstrap, role dispatch
  money.py                  Cents — integer cents only, never float
  storage.py + schema.sql   connection, migration, repository functions
  audit.py                  hash-chained append-only writer + verify_chain()
  auth.py                   argon2id hashing, roles, lockout, @require_role
  rng.py                    secure_seed() + derive_stops() — the ONLY randomness
  payment.py                Luhn + expiry validation, mock tokenisation
  stats.py                  technician diagnostics / commissioner analysis queries
  game/
    spec.py                 GameSpec dataclass + JSON load/validate
    engine.py               evaluate() — pure, deterministic, fully testable
    specs/*.json            Classic Sevens, Five Line Deluxe
  cli/
    menus.py  player.py  technician.py  commissioner.py  prompts.py
  tools/rtpcalc.py          exact RTP + hit-rate calculator
tests/
```

### Money — `money.py`
A `Cents` wrapper over `int` with `parse("12.50") -> 1250`, `__str__ -> "$12.50"`, and explicit
bounds checks against SQLite's signed 64-bit `INTEGER` range.

**No `float` touches a balance anywhere**, and no `Decimal` either — plain integer cents is
simpler to reason about and impossible to drift. This is the single most common real-world
money defect and an obvious Phase II target (float rounding lets an attacker mint fractions of
a cent). A lint check in CI greps for `float(` inside money paths.

### RNG — `rng.py`
```python
def secure_seed() -> bytes:            # secrets.token_bytes(32) — the only entropy source
def derive_stops(seed: bytes, ctx: str, strip_lens: list[int]) -> list[int]:
```
`derive_stops` computes `hmac.new(seed, ctx || reel_index || counter, sha256)` per reel and
reduces the digest to a strip position using **rejection sampling**, not `%`. Modulo on a
non-power-of-two strip length biases common symbols — exactly the kind of subtle unfairness a
gambling commissioner exists to catch, and a good point to raise unprompted in the oral exam.

Python-specific note for the demo: `SecuredRNG.md`'s table names `random()` as the unsafe
Python function and `secrets` as the secure one. **`random` is never imported** anywhere in the
package; a test asserts this by scanning the source tree, which also stops Phase II from
quietly swapping it in.

Every spin (player *and* Commissioner test) goes through this one function. The seed is written
to `spins.seed` unconditionally; only the Commissioner report renders it.

### Game engine — `game/`
A game is **data**, not code:

```python
@dataclass(frozen=True)
class GameSpec:
    id: str; name: str
    reels: list[list[str]]        # one strip per reel; lengths need not match
    rows: int
    paylines: list[list[int]]     # row index per reel
    paytable: list[PayRule]       # (symbol, count, multiplier)
    bet_tiers: list[int]          # cents
    holds: HoldPolicy             # enabled, max_consecutive, cost_fraction
```

Loaded from validated JSON into `games.config_json`. `evaluate(spec, stops, bet)` is pure and
deterministic, so it is fully table-testable.

Configs are **JSON, never `pickle`** — `pickle.load` on attacker-controlled data is arbitrary
code execution, a textbook OWASP A08 / CWE-502 finding and precisely what a Phase II team would
reach for. Worth stating explicitly in the write-up.

**Game A — "Classic Sevens"**: 3 reels × 32-position strips, 1 row, single payline, holds on,
bets $0.25 / $1 / $5, low volatility (frequent small cherry payouts).

**Game B — "Five Line Deluxe"**: 3 rows × 5 reels (24-position strips), no holds, bets
$1 / $5 / $25, high volatility. Five win conditions:
```
line 0  top row        [0,0,0,0,0]
line 1  middle row     [1,1,1,1,1]
line 2  bottom row     [2,2,2,2,2]
line 3  V              [0,1,2,1,0]
line 4  Λ              [2,1,0,1,2]
```
(With 3 rows and 5 reels a straight diagonal cannot span the grid, so the industry-standard
V/Λ zigzags serve as the "diagonals" the spec asks for. Pays on 3, 4 or 5 matching symbols
counting from the leftmost reel.)

### Designing the paytables (the PAR sheet method)

Real slot manufacturers document a machine in a **PAR sheet** (Probability Accounting Report):
reel strips, paytable, RTP and hit frequency in one document. It is the artifact a gambling
commission asks for — which is precisely our Commissioner user group — so `rtpcalc` is
specified to emit one, and the Commissioner interface displays it.

Industry practice sets the targets first and derives the paytable from them, rather than
guessing payouts and measuring what falls out:

1. Fix **target RTP ≈ 94%** and **target hit frequency ≈ 25%** (hit frequency = share of spins
   paying anything, win combinations ÷ total combinations).
2. Allocate the RTP budget across paytable tiers. Frequent low-tier wins typically consume
   **more than half** the budget, which forces the top prize to be genuinely rare — this is the
   non-obvious part, and getting it wrong is how a game ends up above 100% RTP.
3. Adjust **symbol weighting** on the reel strips (how many strip positions each symbol
   occupies) to hit both numbers, then verify exactly with `rtpcalc`.

Our strips are 32 positions (Game A) and 24 (Game B); physical machines classically used 22.
Game A's low-volatility feel comes from a high cherry weighting; Game B's high volatility comes
from a thin top-symbol weighting with a large multiplier.

### RTP calculation — `tools/rtpcalc.py`
Brute-forcing reel positions would be 24⁵ ≈ 8.0M combinations — slow in Python. Instead,
exploit **linearity of expectation** and the independence of reels:

```
E[total payout] = Σ over paylines  E[payout on that line]
E[payout on a line] = Σ over symbol sequences  P(sequence) × pay(sequence)
  where P(sequence) = Π over reels  (count of symbol on reel / strip length)
```
With 8 symbols that is 8⁵ = 32,768 sequences per line, 5 lines ≈ 164k evaluations — exact, not
sampled, and runs in under a second. (This decomposition is valid only because no payout
depends on symbols *off* its own payline; if we ever add scatter symbols, this must revert to
full enumeration. Noted in the module docstring.)

Game A: 8³ = 512 sequences, then a **backwards dynamic program over the hold tree** (depth ≤ 2)
to compute RTP under *optimal* hold play, not merely base play. This matters — a hold is a free
option for the player, so base RTP understates what a smart player extracts. Charging a full
bet per re-spin keeps it in the house's favour, and this tool is how we prove it.

Tune strips until both games land in 92–95%. Save the output as a demo artifact.

### Audit log — `audit.py`
Append-only table; each row stores `prev_hash` and
`hash = sha256(prev_hash || ts || actor || action || detail)`. The genesis row has a zero
`prev_hash`. `verify_chain()` walks the table and reports the first break, surfaced as a
technician command. Hash comparisons use `hmac.compare_digest` (constant-time).

Logged events: registration, login success/failure, lockout, password change, payment method
added, deposit, withdrawal, buy-in, every spin, cash-out, game enable/disable, test session
start/end.

This is our **feature beyond the spec** (PDF §3.1.1 req. 2) and the primary defence for OWASP
A08 and A09.

### Access control — `auth.py`
One `@require_role(Role.TECHNICIAN)` decorator that every privileged menu action carries, with
the audit write happening inside the decorator. A single choke point is far easier to defend in
the oral exam than scattered `if user.is_admin` checks, and far harder for Phase II to quietly
bypass. Tests assert that each role is refused every other role's actions.

Passwords: **argon2id** via `argon2-cffi` (OWASP Password Storage cheat sheet's first choice)
at the OWASP baseline parameters **m = 19456 KiB (19 MiB), t = 2, p = 1**, with a 16-byte salt
from `secrets`. Lockout after 5 failures for 15 minutes. Username comparison and all token
checks use `hmac.compare_digest`. Note `memory_cost` is expressed in KiB, which is a common
place to be off by a factor of 1024.
*Alternative worth weighing:* stdlib `hashlib.scrypt` is also OWASP-approved and would make the
package dependency-free apart from dev tools — a stronger A06 story at the cost of using the
less-recommended algorithm.

### Schema (`schema.sql`)
```
users(id, username UNIQUE, pw_hash, role, must_change_pw,
      failed_count, locked_until, created_at)
accounts(user_id PK, balance_cents, daily_deposit_cents, daily_withdraw_cents, day_bucket)
payment_methods(id, user_id, token, last4, brand, exp_month, exp_year, added_at)
games(id, name, config_json, enabled, created_at)
sessions(id, user_id, game_id, credits_cents, started_at, ended_at)
spins(id, session_id, user_id, game_id, bet_cents, seed BLOB, stops_json,
      payout_cents, is_hold_respin, is_test, created_at)
audit_log(id, ts, actor, action, detail, prev_hash, hash)
```
All access via **`?` bound parameters** (OWASP A03) — never f-strings or `%` formatting in SQL.
Python's `sqlite3` makes the unsafe form syntactically easy, which makes this a concrete thing
to point at on demo day. Connection opened with `isolation_level=None` plus explicit
`BEGIN IMMEDIATE`, so bet → debit → spin → credit is one atomic transaction and a crash can
never leave money half-moved. `PRAGMA foreign_keys=ON` (off by default in SQLite).

`spins.is_test = 1` rows are excluded from player statistics and casino accounting, and
included in Commissioner reports.

### Role interfaces
**Player**: register, login, change password, add/remove payment method, deposit, withdraw,
list games, buy in, spin (with hold prompt in Game A), cash out, view own statistics (games
played, total wagered, total won, net, win %, biggest payout).

**Technician**: login, list/enable/disable games, view paytable and theoretical RTP, system
diagnostic (per game: spins, wins, losses, amount wagered, amount paid, observed vs theoretical
RTP, hit rate, unique players, plays per day), view audit log with filters, `audit verify`.

**Commissioner**: login, per-game statistical analysis (theoretical vs observed RTP, hit rate,
payout distribution histogram, largest win, variance), and **test harness**: run game X for N
rounds at a chosen bet with a virtual bankroll, printing a per-round table of
`round | seed | stops | bet | payout` plus a session summary. Test rounds never touch real
accounts.

## Dependencies
- `argon2-cffi` — password hashing (the only runtime dependency).
- Standard library for everything else: `sqlite3`, `secrets`, `hmac`, `hashlib`, `json`,
  `dataclasses`.
- Dev only: `pytest`, `mypy` (strict on `money`, `game`, `auth`).

`requirements.txt` is pinned with hashes. OWASP A06 is "Vulnerable and Outdated Components",
and a one-line dependency list is the easiest one to defend.

### Installation (the instructor follows this verbatim)
```
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install --require-hashes -r requirements.txt
pip install -e .
slotmachine
```
Python needs a venv where Go would have produced a single binary, so these instructions carry
more weight than they would have. **Rehearse them from a clean clone on a different machine**
before the deadline — any deviation on demo day is noted by the instructor (PDF §3.1.2 req. 1).

## Build order

Bottom-up through the layer stack, with L1/L2 and L3 running in parallel. The critical path is
**L0 → L2 → L4 → L5**; L3 feeds L5 but can be built alongside. Inside L5, build the player CLI
first so there is something demoable, then the technician and commissioner interfaces in
parallel.

## Issue body template

Every layer issue follows the same shape, so the team learns one format:

```markdown
## What this layer is for   — its job in the stack, and what breaks without it
## Depends on / Blocks      — issue links, so GitHub draws the graph
## Spec requirements met    — quoted from Requirement.md / the PDF

## Component A              ← one of these per component in the layer
   Files · Design (signatures, schema, worked examples, sample output)
   Tasks  [ ] commit-sized units
   Tests  [ ] named cases with expected values

## Component B
   ...

## Security requirements    — OWASP category + CWE id + the control, with the failure mode
                              spelled out ("if you use % here, common symbols land more
                              often and the game is provably unfair")
## Acceptance criteria      — [ ] what "done" means, checkable by a reviewer
## Demo-day talking points  — the questions the instructor will ask about this code
```

The per-component sections are what keep a layer assignable to more than one person: a pair
can split L5 by taking the technician and commissioner sections, without needing the issue
to be split up front.

The security and demo sections exist because the PDF grades the oral exam as heavily as the
code (§3.1.2): every team member must be able to defend any part of the system, including
parts they did not write.

## Verification
- `pytest` — payline evaluation across all five lines including near-misses; payout arithmetic;
  RTP calculation against hand-computed values for a tiny fixture game; money arithmetic with
  overflow and negative-balance attempts; the role matrix (each role refused every other role's
  actions); audit chain detects edit, delete and reorder; Luhn validation.
- **Source-scan tests**: assert no `import random`, no `pickle`, and no SQL string
  interpolation anywhere in `src/`. Cheap, and they hold the line during Phase II.
- `slotmachine-rtpcalc` — prints exact RTP and hit rate per game; both must land in 92–95%.
  Capture this output for the demo.
- **Conservation check**: a soak run of ≥100k spins asserting
  `total_wagered − total_paid == casino_profit` exactly, with zero cent drift.
- **Tamper test** (rehearse for the demo): edit one `audit_log` row with the `sqlite3` CLI, then
  run `audit verify` and watch it name the broken entry.
- **Manual walkthrough** against the spec bullets as a literal checklist, from a clean
  `casino.db`, following our own installation instructions verbatim.

## Open risks to revisit
- **Hold EV.** If `rtpcalc`'s optimal-hold analysis puts Game A above ~98% RTP, tighten the Game
  A paytable or reduce `max_consecutive` to 1 before the deadline.
- **"Add or remove" games.** We implement enable/disable. Because configs are stored as JSON
  rows rather than hardcoded, "load a new game from a validated config file" is a small
  addition if the instructor reads "add" strictly — worth keeping in our back pocket.
- **A10 SSRF** almost certainly does not apply to an offline CLI with no outbound requests. The
  PDF requires us to state that rationale explicitly rather than skip it (§4.2 q.11, fn. 4).
