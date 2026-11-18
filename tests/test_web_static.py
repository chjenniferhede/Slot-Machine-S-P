"""Source scans for the browser files, the way test_source_invariants scans the Python.

The pages put text on screen with textContent only. If anyone, including a
Phase II team, slips in an HTML sink or a script from elsewhere, these fail.
The Content Security Policy would block most of it in the browser as well;
this catches it before it ships.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

import pytest

from slotmachine.web.server import STATIC

STATIC_DIR: Final[Path] = Path(__file__).resolve().parent.parent / "src" / "slotmachine" / "web" / "static"

#: Ways to turn a string into markup or code.
SINKS: Final[tuple[str, ...]] = (
    r"\.innerHTML\b",
    r"\.outerHTML\b",
    r"insertAdjacentHTML",
    r"document\.write",
    r"\beval\s*\(",
    r"new\s+Function\b",
    r"setTimeout\s*\(\s*[\"'`]",
    r"setInterval\s*\(\s*[\"'`]",
    r"\.setAttribute\s*\(\s*[\"']on",
    r"\bimport\s*\(",  # dynamic import of a computed URL
)

#: Anything that would make the page load or send something off this machine.
REMOTE: Final[str] = r"(?:https?:)?//[a-z0-9.-]+\.[a-z]{2,}"


def _files(*suffixes: str) -> list[Path]:
    return sorted(p for p in STATIC_DIR.iterdir() if p.suffix in suffixes)


def _code(text: str) -> str:
    """JavaScript with // and /* */ comments removed, so prose does not trip a scan."""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return re.sub(r"(?m)^\s*//.*$|(?<=[;{}),\s])//[^\n\"'`]*$", "", text)


def test_every_static_file_is_served_and_every_served_file_exists():
    served = {name for name, _ in STATIC.values()}
    on_disk = {p.name for p in STATIC_DIR.iterdir() if p.is_file()}
    assert served == on_disk


@pytest.mark.parametrize("path", _files(".js"), ids=lambda p: p.name)
def test_no_html_or_code_sinks(path: Path):
    code = _code(path.read_text(encoding="utf-8"))
    found = [pattern for pattern in SINKS if re.search(pattern, code)]
    assert not found, f"{path.name}: {found}"


@pytest.mark.parametrize("path", _files(".js", ".html", ".css"), ids=lambda p: p.name)
def test_nothing_is_loaded_from_elsewhere(path: Path):
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".js":
        text = _code(text)
    assert re.search(REMOTE, text, flags=re.I) is None, path.name
    assert "@import" not in text


@pytest.mark.parametrize("path", _files(".html"), ids=lambda p: p.name)
def test_html_has_no_inline_script_style_or_handlers(path: Path):
    """Everything inline would need 'unsafe-inline' in the CSP, which we do not grant."""
    html = path.read_text(encoding="utf-8")
    for tag in re.findall(r"<script\b[^>]*>(.*?)</script>", html, flags=re.S | re.I):
        assert tag.strip() == "", f"{path.name}: inline script"
    assert re.search(r"<script\b(?![^>]*\bsrc=)", html, flags=re.I) is None
    assert re.search(r"<style\b", html, flags=re.I) is None
    assert re.search(r"\sstyle\s*=", html, flags=re.I) is None
    assert re.search(r"\son[a-z]+\s*=", html, flags=re.I) is None


@pytest.mark.parametrize("path", _files(".svg"), ids=lambda p: p.name)
def test_svg_has_no_script(path: Path):
    svg = path.read_text(encoding="utf-8")
    assert re.search(r"<script|\son[a-z]+\s*=|javascript:", svg, flags=re.I) is None


def test_math_random_is_cosmetic_only():
    """Math.random may shuffle jokes and coins; it never touches the game."""
    for path in _files(".js"):
        code = _code(path.read_text(encoding="utf-8"))
        for line in code.splitlines():
            if "Math.random" in line:
                assert re.search(r"api\.|fetch\(|bet|seed|stop", line, flags=re.I) is None, (path.name, line)
