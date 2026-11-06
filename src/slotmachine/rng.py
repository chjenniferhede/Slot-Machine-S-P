"""Secure random numbers.

All randomness in this program comes from here. Nothing else should import
``secrets``, and nothing anywhere should import ``random``.

``random`` is unsafe for a slot machine: watch enough of its output and you can
work out its internal state and predict every future value.

A spin works in two steps:

1. Draw a fresh 32-byte seed from the operating system.
2. Turn that seed into reel positions.

Step 2 always gives the same positions for the same seed. So a saved seed can
be replayed later to check a spin was not rigged, even though the spin was
unpredictable at the time. Do not remove that -- it is how we prove the game
is fair.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from typing import Final

SEED_BYTES: Final[int] = 32


def secure_seed() -> bytes:
    """A fresh 32-byte seed from the operating system."""
    return secrets.token_bytes(SEED_BYTES)


def derive_stops(seed: bytes, ctx: str, strip_lens: list[int]) -> list[int]:
    """Work out where each reel stops.

    ``ctx`` labels the spin, so the same seed used for a different spin gives
    different reels. ``strip_lens`` is how many positions each reel has.
    """
    if len(seed) != SEED_BYTES:
        raise ValueError(f"seed must be {SEED_BYTES} bytes, got {len(seed)}")
    return [
        _uniform_below(seed, f"{ctx}|reel{i}", n) for i, n in enumerate(strip_lens)
    ]


def _uniform_below(seed: bytes, label: str, n: int) -> int:
    """Pick a number from 0 to n-1, with every value equally likely.

    Why not just ``value % n``? There are 2**32 possible values. If n does not
    divide into that evenly, the low numbers come up slightly more often, so
    some reel positions would appear more than others. Instead we throw away
    values in the uneven tail and try again. Retries are very rare.

    We use HMAC rather than hashing the seed and label joined together,
    because joining is ambiguous: "reel1" + "0" and "reel10" + "" produce the
    same text.
    """
    if n <= 0:
        raise ValueError("strip length must be positive")

    limit = (2**32 // n) * n  # biggest multiple of n that fits in 32 bits
    counter = 0
    while True:
        digest = hmac.new(seed, f"{label}|{counter}".encode(), hashlib.sha256).digest()
        value = int.from_bytes(digest[:4], "big")
        if value < limit:
            return value % n
        counter += 1  # landed in the tail, try the next one
