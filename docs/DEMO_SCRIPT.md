# Demo-day script (team only — not part of the submission zip)

10 minutes, split evenly. Rehearse with a timer. Start from a **fresh unzip** of the
submitted zip, following INSTALL.md literally.

| Time | Speaker | Do this | Spec item shown |
|---|---|---|---|
| 0:00–1:30 | A | INSTALL.md steps 3–5 (venv, `pip install --require-hashes`, `--no-deps -e .`, `slotmachine --init`). Point out the one-time password. | Installation |
| 1:30–3:30 | A → B | Log in `tech-admin`, forced password change. **Manage games → List** (2 games, 3 bets each, holds, RTP). **Disable** Five Line Deluxe, **enable** it again. **Install** `lucky_bells.json` (prepare beforehand: copy of classic_sevens.json with new id/name). Staff accounts → create commissioner `inspector`. Log out. | Technician auth; add/remove games; ≥2 games; multiple bets |
| 3:30–6:00 | B → C | Register `alice`. Link Visa 4111 1111 1111 1111. Deposit $100. Play Classic Sevens: buy in $20, spin, **hold** a reel, change bet. Cash out. Play Five Line Deluxe: point at the 5 lines / V shapes on the paytable, spin until a win. Cash out, withdraw $10, **My statistics**. Try depositing `-5` to show validation. | Player: register, card, deposit/withdraw, password, bet, holds, multi-line wins, cash out, statistics |
| 6:00–8:00 | C → D | Log in `inspector`, change password. **Test Five Line Deluxe for 5,000 rounds** → seeds table, summary vs theory, save CSV. **Analyse statistics** (live and test). **Replay spin 3** (alice's spin) → Verified. | Commissioner: testing mode, statistics, X-round test with seeds |
| 8:00–10:00 | D | Log in technician. **System diagnostic** (wins/losses per game, frequency of play, system checks). **View audit log** filtered to `alice`. **Verify audit log** → intact. Tamper demo (below) → verify → TAMPERING DETECTED. | Diagnostics, logging, additional feature |

### Using the browser instead

`slotmachine --web` runs the same steps with nicer screens; the routes call the same
functions, so everything above applies. Suggested split: B does the player part in
the browser (lobby, HOLD buttons, coin shower, 中文/EN switch, My stats), C does the
commissioner part in the back office (Test session with seeds, Seed replay tab).
The tamper demonstration works the same: tamper in a second terminal, then press
**Verify hash chain** under Audit log. Keep the terminal visible during installation
so the one-time password is seen being printed there, not in the browser.

## Tamper demonstration (additional feature)

In a second terminal, from the package folder:

```sh
sqlite3 casino.db "UPDATE audit_log SET detail='nothing' WHERE id=5;"
# -> Error: audit_log is append-only: an entry cannot be modified   (trigger blocks it)
sqlite3 casino.db "DROP TRIGGER audit_log_is_immutable; UPDATE audit_log SET detail='nothing' WHERE id=5;"
```

Back in the app: Verify audit log integrity → `✗ TAMPERING DETECTED at entry 5: hash mismatch.`
Do this **last**; afterwards the chain stays broken for that database.
`sqlite3` ships with macOS. Without it (e.g. Windows), with the venv active:

```sh
python -c "import sqlite3; c=sqlite3.connect('casino.db'); c.executescript(\"DROP TRIGGER audit_log_is_immutable; UPDATE audit_log SET detail='nothing' WHERE id=5;\")"
```

## Oral exam prep: every member should be able to

- Walk through `auth.require_role` line by line and explain why it re-reads the role.
- Explain `rng._uniform_below` (rejection sampling, why not `%`).
- Explain why a spin is one transaction (`accounts._play`) and what Ctrl-C does.
- Explain why `Money` is integer cents, and why test spins cannot touch balances (trigger).
- Answer each OWASP category from `docs/OWASP.md`, including why A10 SSRF does not apply.
- Explain the hold price (200%) and why perfect-hold RTP (95.62%) stays under 100%.
- State who wrote which layer (see PHASE_I_TRACKING.md / git log) and what Layer 5 added.
