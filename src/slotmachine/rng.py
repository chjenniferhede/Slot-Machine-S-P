"""Secure random numbers for spins.

Use ``secrets`` here and nowhere else; never use ``random``.

A spin draws a fresh seed, then turns it into reel positions. The same seed
always gives the same positions, so a saved seed replays a spin and shows it
was not rigged.
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
    """Where each reel stops.

    ``ctx`` names the spin, so one seed gives different reels on different
    spins. ``strip_lens`` is how many positions each reel has.
    """
    if len(seed) != SEED_BYTES:
        raise ValueError(f"seed must be {SEED_BYTES} bytes, got {len(seed)}")
    return [
        _uniform_below(seed, f"{ctx}|reel{i}", n) for i, n in enumerate(strip_lens)
    ]


def _uniform_below(seed: bytes, label: str, n: int) -> int:
    """A number from 0 to n-1, with every value equally likely.

    HMAC turns the seed and label into 4 random bytes. Values at or above
    ``limit`` are thrown away and the next counter tried, which keeps every
    reel position equally likely. Retries are rare.
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
        counter += 1
