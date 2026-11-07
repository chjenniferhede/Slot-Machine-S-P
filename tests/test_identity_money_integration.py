"""Identity checks on the actual money APIs, rather than stand-in actions."""

import inspect
from datetime import date

import pytest

from slotmachine import accounts, audit, auth, payment, storage
from slotmachine.cli import menus
from slotmachine.game.spec import dump_spec
from slotmachine.money import Money
from slotmachine.tools.rtpcalc import load_shipped_specs


# Independently enumerate every account action that requires an authenticated player.
PLAYER_ACTIONS = [
    (payment.add_payment_method, (None,)),
    (payment.list_payment_methods, ()),
    (payment.remove_payment_method, (1,)),
    (accounts.get_balance, ()),
    (accounts.deposit, (1, Money(100))),
    (accounts.withdraw, (1, Money(100))),
    (accounts.start_session, ("classic_sevens", Money(100))),
    (accounts.spin, (None, Money(100))),
    (accounts.hold_and_respin, (None, [0])),
    (accounts.get_credits, (None,)),
    (accounts.cash_out, (None,)),
]


def test_all_money_entry_points_have_player_gate():
    actual = {
        fn
        for module in (accounts, payment)
        for name, fn in inspect.getmembers(module, inspect.isfunction)
        if not name.startswith("_") and fn.__module__ == module.__name__
        and next(iter(inspect.signature(fn).parameters), None) == "session"
    }
    assert actual == {fn for fn, _ in PLAYER_ACTIONS}
    for fn in actual:
        assert fn.__required_roles__ == frozenset({auth.Role.PLAYER})


@pytest.mark.parametrize("operation,args", PLAYER_ACTIONS, ids=[fn.__name__ for fn, _ in PLAYER_ACTIONS])
@pytest.mark.parametrize("caller", ["anonymous", "technician", "commissioner", "forced", "stale", "deleted"])
def test_money_actions_deny_invalid_identity_before_business_state(conn, operation, args, caller):
    if caller == "anonymous":
        session = None
    else:
        role = auth.Role(caller) if caller in {"technician", "commissioner"} else auth.Role.PLAYER
        with storage.transaction(conn):
            uid = storage.create_user(conn, "test-user", "unused", role.value, caller == "forced")
            if role == auth.Role.PLAYER:
                storage.create_account(conn, uid)
        session = auth.Session(uid, "test-user", role)
        if caller == "stale":
            conn.execute("UPDATE users SET role = 'technician' WHERE id = ?", (uid,))
        if caller == "deleted":
            conn.execute("DELETE FROM users WHERE id = ?", (uid,))

    def state():
        return {
            name: [tuple(row) for row in conn.execute(f"SELECT * FROM {name} ORDER BY rowid")]
            for name in ("users", "accounts", "payment_methods", "sessions", "spins")
        }

    before = state()
    with pytest.raises(auth.AccessDenied):
        operation(session, conn, *args)
    assert state() == before
    assert not conn.in_transaction
    [event] = audit.read(conn)
    assert event["action"] == audit.Action.ACCESS_DENIED
    assert event["detail"] == operation.__name__
    assert audit.verify_chain(conn).ok


def test_authenticated_player_money_flow(conn):
    password = "A temporary integration passphrase!"
    auth.register_player(conn, "player", password)
    session = auth.login(conn, "PLAYER", password)
    card = payment.CardInput("4111111111111111", 12, date.today().year + 3, "123")
    method = payment.add_payment_method(session, conn, card)
    assert accounts.deposit(session, conn, method.id, Money(10000)) == Money(10000)
    for spec in load_shipped_specs():
        storage.upsert_game(conn, spec.id, spec.name, dump_spec(spec))
    game = accounts.start_session(session, conn, "classic_sevens", Money(5000))
    first = accounts.spin(session, conn, game, Money(100))
    second = accounts.hold_and_respin(session, conn, game, [0])
    assert second.charged == Money(200)
    assert second.held == (0,)
    cashout = accounts.cash_out(session, conn, game)
    assert cashout.amount == Money(5000) - first.charged - second.charged + first.payout + second.payout
    remaining = accounts.get_balance(session, conn)
    assert accounts.withdraw(session, conn, method.id, remaining) == Money.zero()
    payment.remove_payment_method(session, conn, method.id)
    assert payment.list_payment_methods(session, conn) == []
    assert storage.get_open_session(conn, session.user_id) is None
    assert audit.verify_chain(conn).ok


def test_startup_recovers_open_game(conn, tmp_path, capsys):
    from slotmachine.__main__ import main

    password = "A temporary recovery passphrase!"
    uid = auth.register_player(conn, "player", password)
    session = auth.login(conn, "player", password)
    with storage.transaction(conn):
        storage.adjust_balance(conn, uid, Money(10000))
    spec = load_shipped_specs()[0]
    storage.upsert_game(conn, spec.id, spec.name, dump_spec(spec))
    accounts.start_session(session, conn, spec.id, Money(5000))
    db = conn.execute("PRAGMA database_list").fetchone()["file"]
    assert main(["--db", db, "--init"]) == 0
    assert accounts.get_balance(session, conn) == Money(10000)
    assert storage.get_open_session(conn, uid) is None
    assert len(audit.read(conn, action=audit.Action.SESSION_RECOVERED)) == 1
    assert main(["--db", db, "--init"]) == 0
    assert len(audit.read(conn, action=audit.Action.SESSION_RECOVERED)) == 1
    assert password not in capsys.readouterr().out
    assert audit.verify_chain(conn).ok


@pytest.mark.parametrize("error", [auth.AuthError, auth.AccessDenied])
def test_menu_shows_expected_identity_error(error, capsys):
    def denied():
        raise error("Request denied")

    menus.run_action(denied)
    output = capsys.readouterr().out
    assert "Request denied" in output
    assert menus.UNEXPECTED not in output
    assert "Traceback" not in output
