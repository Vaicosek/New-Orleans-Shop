"""The web boundary is tested, not trusted.

Mirrors AbexTech's own test_no_wagering_surface.py, scoped to this site.
Every module under web/ is imported (proving it actually loads without
reaching into the bot or a betting table), then every .py file under web/
is scanned for the six betting tables and the wagering vocabulary
CONTRACT.md section 9 says must never reach the website -- in a route, a
nav tuple, a template string, or anywhere else in the source.
"""
from __future__ import annotations

import importlib
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
WEB_ROOT = ROOT / "web"

FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    if ok:
        print(f"  ok    {name}")
    else:
        FAILS.append(name)
        print(f"  FAIL  {name}  {detail}")


BANNED_TABLES = [
    "pred_markets", "pred_outcomes", "pred_stakes",
    "game_rounds", "game_bets", "gambling_day",
]

# Word-bounded so this cannot flag an innocent word that merely contains one
# of these as a substring (e.g. "alphabet" must never trip on "bet").
BANNED_WORDS = re.compile(
    r"\b(bet|bets|betting|wager|wagers|wagering|casino|casinos|"
    r"gambl\w*|predict\w*|odds|stake|stakes|staked|parimutuel)\b",
    re.IGNORECASE,
)

WEB_MODULES = [
    "web.theme", "web.shell", "web.auth", "web.server",
    "web.pages.storefront", "web.pages.account", "web.pages.ledger",
]

print("importing every module under web/")
for name in WEB_MODULES:
    try:
        importlib.import_module(name)
        check(f"{name} imports cleanly", True)
    except Exception as err:  # noqa: BLE001
        check(f"{name} imports cleanly", False, f"{type(err).__name__}: {err}")

py_files = sorted(WEB_ROOT.rglob("*.py"))
check("found web/ source files to scan", len(py_files) > 0, str(WEB_ROOT))

print("\nscanning web/ source for betting tables and wagering vocabulary")
for path in py_files:
    text = path.read_text(encoding="utf-8")
    rel = path.relative_to(ROOT)

    table_hits = [t for t in BANNED_TABLES if t in text]
    check(f"{rel} references no betting table", not table_hits, ", ".join(table_hits))

    word_hits = sorted({m.group(0).lower() for m in BANNED_WORDS.finditer(text)})
    check(f"{rel} contains no wagering vocabulary", not word_hits, ", ".join(word_hits))

print()
if FAILS:
    print(f"{len(FAILS)} FAILED: {', '.join(FAILS)}")
    sys.exit(1)
print("all no-wagering-on-web tests pass")
