# Jons Hop-Kings Utopia Casino (`slotmachine`)

A secure, auditable slot machine for the Security & Privacy in Computing course
project: three roles (player, technician, commissioner), two games, a hash-chained
audit log, and a local web client. No real money is involved.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install --require-hashes -r requirements.txt
.venv/bin/python -m pip install --no-deps .
.venv/bin/slotmachine --web
```

On Windows use `.venv\Scripts\python` and `.venv\Scripts\slotmachine`.

## Documents

- [Installation](docs/INSTALL.md)
- [User manual](docs/MANUAL.md)
- [OWASP Top 10 write-up](docs/OWASP.md)
- [Implementation plan](docs/PLAN.md)
- [Delivery tracker and remaining work](docs/PHASE_I_TRACKING.md)
- [Identity layer notes](docs/IDENTITY.md)
- [GitHub tracking issue](https://github.com/chjenniferhede/Slot-Machine-S-P/issues/8)
