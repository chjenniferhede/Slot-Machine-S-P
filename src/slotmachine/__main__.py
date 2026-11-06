"""Entry point.

Layer 0 provides a stub so the console script is verifiable from day one.
Layer 2 (#3) replaces this with the first-run bootstrap and role dispatch.
"""

from __future__ import annotations

import argparse
import sys

from slotmachine import __version__
from slotmachine import storage


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="slotmachine", description="Big Casino slot machine")
    parser.add_argument("--version", action="version", version=f"slotmachine {__version__}")
    parser.add_argument(
        "--db", default=storage.DEFAULT_DB_PATH, help="database file (default: casino.db)"
    )
    parser.add_argument(
        "--init", action="store_true", help="create or upgrade the database, then exit"
    )
    args = parser.parse_args(argv)

    if args.init:
        conn = storage.connect(args.db)
        try:
            storage.init_db(conn)
            print(f"Initialised {args.db}")
        finally:
            conn.close()
        return 0

    print(f"slotmachine {__version__}")
    print("Interfaces arrive with Layer 2 (#3) and Layer 5 (#6). Use --init to set up the database.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
