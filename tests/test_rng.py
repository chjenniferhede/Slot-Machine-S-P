"""Secure random number generation."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from slotmachine.rng import (
    SEED_BYTES,
    _uniform_below,
    derive_stops,
    random_token,
    secure_seed,
)

SRC = Path(__file__).resolve().parent.parent / "src" / "slotmachine"


def source(path: Path) -> str:
    # Explicit UTF-8: the default follows the machine's locale, which on a
    # Chinese Windows install is cp936 and cannot read the box-drawing
    # characters in engine.py.
    return path.read_text(encoding="utf-8")


def test_seed_length_and_type() -> None:
    seed = secure_seed()
    assert isinstance(seed, bytes)
    assert len(seed) == SEED_BYTES


def test_determinism() -> None:
    """The replay property: a logged seed must reproduce the logged stops."""
    seed = secure_seed()
    first = derive_stops(seed, [32, 32, 32])
    for _ in range(100):
        assert derive_stops(seed, [32, 32, 32]) == first


def test_different_seeds_differ() -> None:
    """Distinct seeds must give distinct stops.

    Measured over 8 reels rather than 3. With 3 reels there are only 32,768
    possible results, so about 15 of 1000 draws would collide by chance and
    the test would be measuring that rather than the generator. With 8 reels
    there are 1.1 trillion, so a collision is vanishingly unlikely.
    """
    seeds = [hashlib.sha256(str(i).encode()).digest() for i in range(1000)]
    results = {tuple(derive_stops(s, [32] * 8)) for s in seeds}
    assert len(results) == len(seeds)


def test_reels_are_independent() -> None:
    """One seed must give a different answer for each reel.

    Without the per-reel label every reel would ask HMAC the same question
    and get the same answer, so every spin would show three matching symbols.
    """
    all_three_same = 0
    for i in range(200):
        seed = hashlib.sha256(str(i).encode()).digest()
        stops = derive_stops(seed, [32, 32, 32])
        if stops[0] == stops[1] == stops[2]:
            all_three_same = all_three_same + 1
    assert all_three_same < 5


def test_rejects_bad_seed_length() -> None:
    with pytest.raises(ValueError):
        derive_stops(b"short", [32])


def test_rejects_zero_length_strip() -> None:
    with pytest.raises(ValueError):
        _uniform_below(secure_seed(), "x", 0)
    with pytest.raises(ValueError):
        _uniform_below(secure_seed(), "x", -5)


@pytest.mark.parametrize("n", [2, 3, 22, 24, 32])
def test_all_positions_reachable(n: int) -> None:
    seed = secure_seed()
    seen = {_uniform_below(seed, f"label{i}", n) for i in range(n * 200)}
    assert seen == set(range(n))


def test_uniformity_chi_squared() -> None:
    """24 is not a power of two, so a naive ``% n`` would bias low positions.

    Driven from one fixed seed with varying labels so the sample is identical
    on every run -- a statistical test that fails one run in a thousand will
    eventually fail in CI for no reason.
    """
    n = 24
    draws = 100_000
    seed = bytes(range(SEED_BYTES))

    counts = [0] * n
    for i in range(draws):
        counts[_uniform_below(seed, f"u{i}", n)] += 1

    expected = draws / n
    chi_squared = sum((c - expected) ** 2 / expected for c in counts)

    # Critical value for 23 degrees of freedom at p = 0.001.
    assert chi_squared < 49.728, f"chi-squared {chi_squared:.2f} suggests a biased draw"


def test_random_token_shape_and_uniqueness() -> None:
    tokens = {random_token() for _ in range(1000)}
    assert len(tokens) == 1000
    for token in tokens:
        assert len(token) == 32
        assert set(token) <= set("0123456789abcdef")
    assert len(random_token(32)) == 64


def test_random_token_refuses_too_little_entropy() -> None:
    with pytest.raises(ValueError):
        random_token(8)


def test_no_insecure_random_anywhere() -> None:
    """``random`` must not be imported anywhere in the package.

    This is the test that stops a predictable generator being substituted --
    by a Phase II team, or by one of us at 2am.
    """
    offenders = [
        str(path.relative_to(SRC))
        for path in SRC.rglob("*.py")
        if "import random" in source(path) or "from random import" in source(path)
    ]
    assert not offenders, f"insecure random imported in: {offenders}"


def test_entropy_drawn_in_one_place() -> None:
    """``secrets`` and ``os.urandom`` belong in rng.py and nowhere else yet."""
    allowed = {"rng.py"}
    offenders = [
        str(path.relative_to(SRC))
        for path in SRC.rglob("*.py")
        if path.name not in allowed
        and ("secrets" in source(path) or "urandom" in source(path))
    ]
    assert not offenders, f"entropy drawn outside rng.py: {offenders}"
