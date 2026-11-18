# Installation

Jons Hop-Kings Utopia Casino (package `slotmachine`) runs on one machine and is
used through a web browser on that same machine. It never listens on the
network and never makes outbound requests.

## Requirements

- Python 3.11 or newer (`python --version` on Windows, `python3 --version` elsewhere)
- A current browser (Edge, Chrome, Firefox or Safari)
- About 50 MB of disk space

## Install

Run these from the folder that contains `pyproject.toml`, in order.

macOS / Linux:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install --require-hashes -r requirements.txt
.venv/bin/python -m pip install --no-deps .
```

Windows (PowerShell):

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install --require-hashes -r requirements.txt
.venv\Scripts\python -m pip install --no-deps .
```

`--require-hashes` refuses any dependency whose file does not match the hash
recorded in `requirements.txt`. The only runtime dependency is `argon2-cffi`
(with its `argon2-cffi-bindings` and `cffi`/`pycparser` helpers).

## First run

macOS / Linux:

```sh
.venv/bin/slotmachine --web
```

Windows:

```powershell
.venv\Scripts\slotmachine --web
```

The first run creates `casino.db` in the current folder, installs the two
shipped games, and creates the first technician account. **It prints that
account's password once, in the terminal:**

```
slotmachine 0.1.0
Technician account created
  username: tech-admin
  password: (24 random characters)
This password is shown once. Save it now; you must change it at first login.
Casino open at http://127.0.0.1:8888/  (Ctrl-C to close)
```

Write the password down before doing anything else. It is never shown again,
never stored in the clear, and never sent to the browser. A browser window
opens at `http://127.0.0.1:8888/`. If it does not, open that address yourself.

Options:

| Option | Meaning |
| --- | --- |
| `--db PATH` | Use a different database file (default `casino.db`) |
| `--port N` | Serve on another port, 1024–65535 (default 8888) |
| `--no-browser` | Do not open a browser window |
| `--init` | Create the database and the technician, print the password, and exit |

Press Ctrl-C in the terminal to close the casino. Credits left in a game
when the program stops are returned to the player's balance at the next start.

## Verify the installation

1. Log in as `tech-admin` with the printed password. You are asked to choose
   a new password (at least 12 characters), then land in the technician back office.
2. Open **Audit log** and press **Verify hash chain**: it reports the chain intact.
3. Optional, from the terminal: the payout calculator prints both games' PAR sheets.

   ```sh
   .venv/bin/slotmachine-rtpcalc          # Windows: .venv\Scripts\slotmachine-rtpcalc
   ```

## For developers

Install the test tools as well, and the package in editable mode:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install --require-hashes -r requirements.txt -r requirements-dev.txt
.venv/bin/python -m pip install --no-deps -e .
.venv/bin/python -m pytest              # about 2 minutes
.venv/bin/python -m pytest -m slow      # about 20 minutes: 100k-spin conservation, 1M-spin RTP
.venv/bin/python -m mypy
```

On Windows use `.venv\Scripts\python` in place of `.venv/bin/python`.

To fill a throwaway database with play history for a demonstration:

```sh
.venv/bin/python tools/demo_seed.py --db demo.db
.venv/bin/slotmachine --web --db demo.db
```

Never commit a database file: it holds password hashes and payment tokens.

## Uninstall

Delete the `.venv` folder and any `*.db` files you created.
