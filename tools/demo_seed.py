"""Fill a throwaway database with play history, so the reports have something to show.

    python tools/demo_seed.py --db demo.db [--players 4] [--spins 150]

Everything goes through the same role-gated functions the web pages call:
players register, add the published test card 4242 4242 4242 4242, deposit,
buy in, spin and cash out. The demo players' passwords are random and printed
once at the end. Use the result for rehearsal only and never commit it.
"""

from __future__ import annotations

import argparse
import secrets
import sys
from datetime import datetime, timezone

from slotmachine import accounts, auth, payment, storage
from slotmachine.__main__ import startup
from slotmachine.money import Money

TEST_CARD = "4242424242424242"  # a payment processor's published test number


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", required=True, help="database to create or extend (not casino.db)")
    parser.add_argument("--players", type=int, default=4)
    parser.add_argument("--spins", type=int, default=150, help="spins per player per game, at most")
    args = parser.parse_args(argv)
    if args.db.endswith("casino.db"):
        parser.error("seed a separate demo database, not casino.db")

    conn = storage.connect(args.db)
    try:
        bootstrap = startup(conn)
        if bootstrap is not None:
            print(f"tech-admin one-time password: {bootstrap}")
        year = datetime.now(timezone.utc).year + 3
        made: list[tuple[str, str]] = []
        for number in range(1, args.players + 1):
            name = f"demo_player{number}"
            if storage.get_user_by_username(conn, name) is not None:
                continue
            phrase = secrets.token_urlsafe(12)
            auth.register_player(conn, name, phrase)
            who = auth.login(conn, name, phrase)
            card = payment.add_payment_method(who, conn, payment.CardInput(TEST_CARD, 12, year, "123"))
            accounts.deposit(who, conn, card.id, Money(50_000))
            for spec in accounts.list_playable_games(who, conn):
                bet = Money(spec.bet_tiers[0])
                gs = accounts.start_session(who, conn, spec.id, Money(bet.cents * 40))
                for _ in range(args.spins):
                    if accounts.get_credits(who, conn, gs) < bet:
                        break
                    accounts.spin(who, conn, gs, bet)
                accounts.cash_out(who, conn, gs)
            made.append((name, phrase))
            print(f"  {name} played")
    finally:
        conn.close()

    if made:
        print("\nDemo players (passwords shown once):")
        for name, phrase in made:
            print(f"  {name:<16} {phrase}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
