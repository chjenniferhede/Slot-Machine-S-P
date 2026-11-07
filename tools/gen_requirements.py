#!/usr/bin/env python3
"""Regenerate requirements.txt and requirements-dev.txt with hashes.

Run after changing a pinned version:

    python tools/gen_requirements.py

Fetches every distribution PyPI publishes for each pinned version and records
all of their SHA-256 hashes. Listing all of them -- not just the wheel that
matches this machine -- is what keeps the install working on Linux, Windows and
both Mac architectures. A requirements file pinned to one platform's wheel
fails everywhere else, which on demo day means the install fails in front of
the instructor.

Transitive dependencies must be listed explicitly: pip's --require-hashes mode
refuses to install anything it has no hash for.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

RUNTIME: list[tuple[str, str]] = [
    ("argon2-cffi", "23.1.0"),
    ("argon2-cffi-bindings", "26.1.0"),
    ("cffi", "2.1.1"),
    ("pycparser", "3.12"),
]

DEV: list[tuple[str, str]] = [
    ("pytest", "8.3.3"),
    ("mypy", "1.11.2"),
    ("iniconfig", "2.0.0"),
    ("packaging", "24.1"),
    ("pluggy", "1.5.0"),
    ("mypy-extensions", "1.0.0"),
    ("typing-extensions", "4.12.2"),
    ("colorama", "0.4.6"),  # Required by pytest on Windows.
]

ROOT = Path(__file__).resolve().parent.parent


def hashes_for(name: str, version: str) -> list[str]:
    url = f"https://pypi.org/pypi/{name}/{version}/json"
    try:
        with urllib.request.urlopen(url, timeout=30) as response:
            data = json.load(response)
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"{name}=={version}: not found on PyPI ({exc.code})") from exc

    digests = sorted({f["digests"]["sha256"] for f in data["urls"]})
    if not digests:
        raise SystemExit(f"{name}=={version}: no distributions published")
    return digests


def render(pkgs: list[tuple[str, str]]) -> str:
    blocks = []
    for name, version in pkgs:
        lines = [f"{name}=={version} \\"]
        lines += [f"    --hash=sha256:{h} \\" for h in hashes_for(name, version)]
        lines[-1] = lines[-1].rstrip(" \\")
        blocks.append("\n".join(lines))
    return "\n".join(blocks) + "\n"


def main() -> int:
    runtime_header = (ROOT / "requirements.txt").read_text().split("\nargon2-cffi==")[0] + "\n"
    dev_header = (ROOT / "requirements-dev.txt").read_text().split("\npytest==")[0] + "\n"

    (ROOT / "requirements.txt").write_text(runtime_header + render(RUNTIME))
    (ROOT / "requirements-dev.txt").write_text(dev_header + render(DEV))
    print("Regenerated requirements.txt and requirements-dev.txt")
    return 0


if __name__ == "__main__":
    sys.exit(main())
