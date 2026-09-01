"""The web boundary is tested, not trusted.

Mirrors AbexTech's own test_no_wagering_surface.py, scoped to this site.

This used to be a text scan: literal betting-table names plus a fixed word
list, checked against source text. That scan passed on a real, working
wagering page -- `from core.games import recent_rounds`, an `<h1>` reading
"Coinflip & dice results", a route registered at `/rounds` -- because the
word list never had "game"/"round"/"coinflip"/"house"/"payout"/"outcome" in
it, and because nothing here ever looked at what the app actually imports or
actually serves. A guard that checks the words a developer typed, rather
than the code that runs, is exactly as strong as that developer's vocabulary.

Two checks now do the real work, on the running system rather than its
source text:

1. IMPORT GRAPH. Every `.py` file under `web/` is discovered (not hand-listed
   -- a new page module is caught automatically) and imported for real, in
   this process. `core.games` and `core.predictions` are the only two
   modules in the whole codebase that touch a betting table (CONTRACT.md
   S4/S9), so after every web module has been imported, neither name may
   appear anywhere in `sys.modules` -- if it does, some import chain starting
   under `web/` reached it, directly or through any number of hops.

2. ROUTE ALLOWLIST. `create_app()` is actually called and `app.router` is
   actually enumerated. Every registered path must be one CONTRACT.md S12
   names. A route nobody put in the contract -- `/rounds`, say -- fails here
   even if its handler, its imports and its template text are all otherwise
   clean.

The betting-table/vocabulary text scan stays on as a second, cheaper layer
(now with the missing words), but it is a defense in depth, not the guard --
the two checks above are what actually cannot be talked past by rewording.
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
# Expanded from the original list, which omitted exactly the words a real
# casino page would use: game(s), round, coinflip, dice, house, payout,
# outcome.
BANNED_WORDS = re.compile(
    r"\b(bet|bets|betting|wager|wagers|wagering|casino|casinos|"
    r"gambl\w*|predict\w*|odds|stake|stakes|staked|parimutuel|"
    r"game|games|round|rounds|coinflip|dice|house|payout|payouts|outcome|outcomes)\b",
    re.IGNORECASE,
)

# The two modules in the whole codebase allowed to touch a betting table
# (CONTRACT.md S4, S9). Nothing reachable from web/ may import either.
BANNED_CORE_MODULES = ("core.games", "core.predictions")

# CONTRACT.md S12 -- the entire, exact route surface this website may serve.
ALLOWED_ROUTES = {
    "/health", "/login", "/auth/callback", "/logout",
    # /inventory is the page's name; /stock is the name it had before the
    # owner renamed it and stays served so an already-pasted link keeps working.
    "/", "/inventory", "/stock", "/me", "/ledger", "/order",
}


def _web_module_names() -> list[str]:
    """Every `.py` file under `web/`, as a dotted module name.

    Discovered from disk, not hand-maintained -- a module nobody remembered
    to add to a list is exactly how the wagering page in the audit went
    unnoticed by the old version of this test.
    """
    names = []
    for path in sorted(WEB_ROOT.rglob("*.py")):
        rel = path.relative_to(ROOT).with_suffix("")
        parts = rel.parts
        if "__pycache__" in parts:
            continue
        if parts[-1] == "__init__":
            parts = parts[:-1]
        names.append(".".join(parts))
    return names


WEB_MODULES = _web_module_names()

print("importing every module under web/ (discovered from disk)")
before_modules = set(sys.modules)
already_present = {m for m in BANNED_CORE_MODULES if m in before_modules}
check("core.games / core.predictions are not already loaded before we start "
      "(otherwise the import-graph check below would be meaningless)",
      not already_present, ", ".join(sorted(already_present)))

for name in WEB_MODULES:
    try:
        importlib.import_module(name)
        check(f"{name} imports cleanly", True)
    except Exception as err:  # noqa: BLE001
        check(f"{name} imports cleanly", False, f"{type(err).__name__}: {err}")

after_modules = set(sys.modules)

print("\nchecking the import graph reachable from web/")
newly_imported = after_modules - before_modules
reached_betting_core = {
    m for m in newly_imported
    if m in BANNED_CORE_MODULES
    or any(m == banned or m.startswith(banned + ".") for banned in BANNED_CORE_MODULES)
}
check("no module reachable from web/ imports core.games or core.predictions "
      "(checked against sys.modules after importing every web/ file, so this "
      "catches an indirect import through any number of hops, not just a "
      "direct one)",
      not reached_betting_core, ", ".join(sorted(reached_betting_core)))

print("\nenumerating app.router against the CONTRACT.md S12 allowlist")
try:
    from web.server import create_app
    app = create_app()
    registered = {
        route.resource.canonical
        for route in app.router.routes()
        if route.resource is not None
    }
    unexpected = registered - ALLOWED_ROUTES
    missing = ALLOWED_ROUTES - registered
    check("app.router registers no route outside the CONTRACT.md S12 allowlist "
          "(a page like /rounds fails here even if its own source is clean)",
          not unexpected, ", ".join(sorted(unexpected)))
    check("every route CONTRACT.md S12 promises is actually registered",
          not missing, ", ".join(sorted(missing)))
except Exception as err:  # noqa: BLE001
    check("create_app() builds and its routes can be enumerated", False,
          f"{type(err).__name__}: {err}")

py_files = sorted(WEB_ROOT.rglob("*.py"))
check("found web/ source files to scan", len(py_files) > 0, str(WEB_ROOT))

print("\nscanning web/ source for betting tables and wagering vocabulary "
      "(defense in depth, not the primary guard)")
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
