# Installation — Big Casino Secure Slot Machine

Follow these steps in order, exactly as written. They have been tested from a
freshly unzipped copy of this package on Linux with Python 3.11, 3.12 and 3.13.

> **Language note.** The project specification names Go. Per the instructor's
> guidance for this team, the system is written in **Python 3.11+** instead.
> Python needs a virtual environment and one `pip install` where Go would have
> needed `go mod tidy`; the steps below are the full equivalent.

## 1. Requirements

| Requirement | Version | Check with |
|---|---|---|
| Python | **3.11 or newer** (3.11, 3.12, 3.13 tested) | `python3 --version` |
| pip | comes with Python | `python3 -m pip --version` |
| Internet access | for step 4 only (downloads one library from PyPI) | |

There is no separate database to install. SQLite is built into Python.

### If `python3 --version` shows 3.10 or older

macOS ships an older Python. Install a newer one with **one** of:

- **python.org installer (simplest):** download Python 3.12 from
  <https://www.python.org/downloads/macos/>, run it, then open a new terminal and
  use `python3.12` in place of `python3` in step 3.
- **Homebrew:** `brew install python@3.12`, then use `python3.12` in step 3.

On Windows, install Python 3.12 from <https://www.python.org/downloads/windows/>
and tick **"Add python.exe to PATH"**. Use `py -3.12` in place of `python3`.

## 2. Unzip the package and open a terminal in it

```sh
unzip Slot-Machine-S-P.zip
cd Slot-Machine-S-P
```

Every command below is run from this folder (the one containing `pyproject.toml`).

## 3. Create and activate a virtual environment

macOS / Linux:

```sh
python3 -m venv .venv
source .venv/bin/activate
```

Windows (PowerShell):

```powershell
py -3.12 -m venv .venv
.venv\Scripts\Activate.ps1
```

Your prompt now starts with `(.venv)`. Inside the virtual environment `python`
is the correct version, whatever the system default is.

## 4. Install the dependencies and the program

```sh
python -m pip install --upgrade pip
python -m pip install --require-hashes -r requirements.txt
python -m pip install --no-deps -e .
```

- The first line updates pip so it can read the hash-pinned file.
- The second installs the only runtime dependency, `argon2-cffi` (password
  hashing), and its two sub-dependencies. `--require-hashes` makes pip refuse
  any file whose SHA-256 does not match the one recorded in `requirements.txt`,
  so a tampered or substituted package cannot be installed.
- The third installs the `slotmachine` program itself. `--no-deps` stops pip from
  fetching anything that step two did not verify. (`-e` keeps the program linked to
  this folder. A plain `pip install --no-deps .` also works: the database schema,
  the game files and the web pages are packaged with it.)

## 5. Initialise the database and save the technician password

```sh
slotmachine --init
```

This creates `casino.db` in the current folder, installs the two shipped games
(Classic Sevens and Five Line Deluxe), and creates the first technician account.
It prints something like:

```
slotmachine 0.1.0
Technician account created
  username: tech-admin
  password: q2X...random...9Lw
This password is shown once. Save it now; you must change it at first login.
Initialised casino.db
```

**Copy that password now.** It is random, is never shown again, and is not stored
anywhere in readable form. (If it is lost, delete `casino.db` and run
`slotmachine --init` again; this erases all data.)

## 6. Run the slot machine

```sh
slotmachine
```

You see:

```
slotmachine 0.1.0

  BIG CASINO SLOT MACHINE
1 Log in | 2 Register player | 3 Quit:
```

Continue with the user manual, [`MANUAL.md`](MANUAL.md). Section 2 of the manual
is a five-minute first-run walkthrough that creates one account of each role.

`python -m slotmachine` works the same as `slotmachine`. To use a database file
in another place, add `--db path/to/file.db`.

### Or open the casino in a browser

The same program has a web front end for all three roles. It uses only Python's
standard library, so there is nothing more to install:

```sh
slotmachine --web
```

It prints `Casino open at http://127.0.0.1:8888/` and opens that page in your
browser (open it yourself if it does not). Everyone logs in on that page; staff
are taken to the back office. Press Ctrl-C in the terminal to close it.

| Option | Meaning |
|---|---|
| `--port N` | Serve on another port, 1024–65535 (default 8888) |
| `--no-browser` | Do not open a browser window |
| `--db path/to/file.db` | Use another database, as above |

The server listens on `127.0.0.1` only and cannot be reached from other machines.
The technician's one-time password is printed in the terminal, never in the browser.
The terminal menus and the browser use the same database and the same rules; use
whichever you prefer, but run only one copy of the program per database at a time.

## 7. Optional: run the test suite

```sh
python -m pip install --require-hashes -r requirements-dev.txt
python -m pytest              # about 770 tests, under a minute
python -m pytest -m slow      # 4 long soak/RTP tests, about 2–3 minutes
python -m mypy                # strict type check
slotmachine-rtpcalc           # exact RTP and PAR sheet for each shipped game
```

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `ERROR: Package 'slotmachine' requires a different Python: 3.10...` | The venv was made with an old Python. Delete `.venv` and redo step 3 with `python3.12`. |
| `command not found: slotmachine` | The virtual environment is not active. Run `source .venv/bin/activate` (Windows: `.venv\Scripts\Activate.ps1`), or use `python -m slotmachine`. |
| `THESE PACKAGES DO NOT MATCH THE HASHES...` | The download was altered or corrupted. Retry on a trusted network; do not remove `--require-hashes`. |
| PowerShell refuses to run `Activate.ps1` | Run `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` once, then activate again. |
| `The database is unavailable.` | The folder is read-only or the `--db` path does not exist. Run from a folder you can write to. |
| Forgot the tech-admin password | Delete `casino.db` (all data is lost) and run `slotmachine --init` again. |

## Removing it

Deactivate with `deactivate`, then delete the folder. Everything the program
writes (`casino.db`, `casino.db-wal`, `casino.db-shm`, and `reports/`) is inside it.
