"""Cryptographically secure randomness.

This module is the ONLY source of entropy in the program. Nothing else may
import ``secrets`` or ``os.urandom``, and nothing anywhere may import
``random``.

ProvidedResources/SecuredRNG.md names ``random()`` as Python's unsafe function
and ``secrets`` as the secure one. The reason is stronger than "low quality":
``random`` is a Mersenne Twister, and observing 624 consecutive outputs reveals
its entire internal state, making every future value predictable. It is a
reversible generator, not merely a weak one.

THE REPLAY PROPERTY
-------------------
Every spin draws a fresh 32-byte seed and derives its reel stops from that seed
deterministically. The seed is recorded with the spin. This gives two
properties that look contradictory but are not:

* Play is unpredictable, because the seed is fresh OS entropy that nobody sees
  before the spin resolves.
* Any recorded spin is verifiable, because feeding the logged seed back through
  ``derive_stops`` must reproduce the logged stops exactly.

That is how the spec can demand both cryptographically secure generation and
that Commissioners see "the random seeds used for each round".

Do not "optimise" the determinism away. It is the fairness guarantee.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from typing import Final

SEED_BYTES: Final[int] = 32


def secure_seed() -> bytes:
    """A fresh 32-byte seed from the operating system's CSPRNG."""
    return secrets.token_bytes(SEED_BYTES)


def derive_stops(seed: bytes, ctx: str, strip_lens: list[int]) -> list[int]:
    """Derive one reel stop per strip, deterministically, from a seed.

    The same seed, ctx and strip lengths always produce the same stops, which
    is what makes a recorded spin replayable.
    """
    if len(seed) != SEED_BYTES:
        raise ValueError(f"seed must be {SEED_BYTES} bytes, got {len(seed)}")
    return [
        _uniform_below(seed, f"{ctx}|reel{i}", n) for i, n in enumerate(strip_lens)
    ]


def _uniform_below(seed: bytes, label: str, n: int) -> int:
    """A uniformly distributed integer in [0, n), with no modulo bias.

    WHY NOT ``value % n``
    ---------------------
    A 32-bit value is uniform over 0 .. 4294967295, which is 2**32 values. With
    a 24-position reel strip, 2**32 / 24 is not a whole number: remainders 0-15
    occur 178956971 times and remainders 16-23 occur 178956970 times. The first
    sixteen positions on the reel come up slightly more often than the last
    eight.

    At 32 bits that bias is about one part in 179 million -- genuinely
    negligible. But the identical code with a smaller source or a larger n
    produces a bias big enough to move the RTP, and it looks exactly the same.
    Rejection sampling removes the question: discard values in the uneven tail
    and try again. The expected number of retries here is below 1.000001, so it
    costs nothing.

    WHY HMAC rather than sha256(seed + label)
    -----------------------------------------
    Plain concatenation is ambiguous -- "reel1" + "0" and "reel10" + "" produce
    the same bytes -- and the construction is open to length extension. HMAC
    separates key from message, so distinct labels give independent outputs.
    """
    if n <= 0:
        raise ValueError("strip length must be positive")

    limit = (2**32 // n) * n  # largest multiple of n that fits in 32 bits
    counter = 0
    while True:
        digest = hmac.new(seed, f"{label}|{counter}".encode(), hashlib.sha256).digest()
        value = int.from_bytes(digest[:4], "big")
        if value < limit:
            return value % n
        counter += 1
