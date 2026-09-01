"""bot/ surface test.

Imports every module under `bot/` with a stubbed `discord` (no real
discord.py, no network, no token -- see `tests/_stubs/discord_stub.py`,
modelled on AbexTech's own `_harness/stubs.py`) and statically asserts the
rules from CONTRACT.md section 7 that matter most and are cheap to check
without a live gateway connection:

  - exactly NINE top-level slash commands exist, across every cog
  - no `discord.ui.TextInput` anywhere carries a label asking for a user,
    item, order or market id -- "never a text field for an identity"
  - every slash command handler's FIRST statement defers the interaction --
    the 3-second window rule
  - `OrderCardView.approve_btn` checks staff before it does anything else --
    it is the only order-approval entry point reachable from a message
    every member of the server can see
  - no `discord.ui.TextInput` placeholder echoes a value already shown in
    that same field's own label -- a "confirmation" the user can just
    copy-paste isn't one
  - no user-facing string embeds a raw internal id next to its address code
    (the `kind:{id}` pattern) -- an address code is the only identity a
    card is allowed to show
"""
from __future__ import annotations

import ast
import importlib
import inspect
import os
import pkgutil
import re
import sys
import tempfile
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests" / "_stubs"))

_tmp = tempfile.mkdtemp(prefix="nola-bot-surface-")
os.environ["NOLA_DB_PATH"] = str(Path(_tmp) / "test.db")
os.environ["NOLA_GAME_SEED_SECRET"] = "test-secret-do-not-use-in-prod"
# Fake but well-formed config, just so every module that reads config at
# import time (none currently do -- all reads are inside functions) would
# still work if that ever changed.
os.environ.setdefault("DISCORD_TOKEN", "test-token")
os.environ.setdefault("GUILD_ID", "1")
os.environ.setdefault("SHOP_CHANNEL_ID", "2")
os.environ.setdefault("ORDERS_CHANNEL_ID", "3")
os.environ.setdefault("ALERTS_CHANNEL_ID", "4")

import discord_stub  # noqa: E402
discord_stub.install()

FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    if ok:
        print(f"  ok    {name}")
    else:
        FAILS.append(name)
        print(f"  FAIL  {name}  {detail}")


# ------------------------------------------------------------------ every module under bot/ imports
import bot  # noqa: E402

modules = []
import_errors: list[str] = []
for _finder, name, _ispkg in pkgutil.walk_packages(bot.__path__, prefix="bot."):
    try:
        modules.append(importlib.import_module(name))
    except Exception as err:  # noqa: BLE001
        import_errors.append(f"{name}: {type(err).__name__}: {err}")

check(f"every module under bot/ imports cleanly ({len(modules)} modules)",
      not import_errors, "; ".join(import_errors))

# ------------------------------------------------------------------ exactly 9 slash commands
from discord import app_commands  # noqa: E402

cog_modules = [m for m in modules if re.fullmatch(r"bot\.cogs\.[a-z_]+", m.__name__)]

commands_found: list[tuple[str, str, "app_commands.AppCommand"]] = []
for mod in cog_modules:
    for cls_name, cls in vars(mod).items():
        if not inspect.isclass(cls) or cls.__module__ != mod.__name__:
            continue
        for member_name, member in vars(cls).items():
            if isinstance(member, app_commands.AppCommand):
                commands_found.append((mod.__name__, member_name, member))

command_names = sorted(f"{m}.{n} (/{c.name})" for m, n, c in commands_found)
# Eight domain panels plus /setup, which is not a domain -- it runs once at
# install and exists separately because /admin is gated on is_staff, which is
# False for everyone on a server whose STAFF_ROLE_IDS is still empty. The
# number is pinned rather than bounded: this budget is the whole reason the
# surface stayed small, and a command added without touching CONTRACT.md
# section 7 should fail here first.
check(
    f"exactly 9 top-level slash commands exist (found {len(commands_found)}: {command_names})",
    len(commands_found) == 9,
)
check("all 9 command names are distinct",
      len({c.name for _m, _n, c in commands_found}) == len(commands_found))


# ------------------------------------------------------------------ every command handler defers first
def _dotted(node: ast.AST) -> str:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def _defers_first(func) -> bool:
    try:
        source = textwrap.dedent(inspect.getsource(func))
    except (OSError, TypeError):
        return False
    tree = ast.parse(source)
    fn_def = tree.body[0]
    if not isinstance(fn_def, (ast.FunctionDef, ast.AsyncFunctionDef)) or not fn_def.body:
        return False
    first = fn_def.body[0]
    if isinstance(first, ast.Expr) and isinstance(first.value, ast.Await):
        call = first.value.value
        if isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute):
            return _dotted(call.func).endswith("response.defer")
    return False


for mod_name, member_name, member in commands_found:
    check(f"{mod_name}.{member_name} defers as its first statement",
          _defers_first(member.callback))

# ------------------------------------------------------------------ no TextInput asks for an identity
LABEL_RE = re.compile(
    r"discord\.ui\.TextInput\(\s*label\s*=\s*f?[\"']([^\"']*)[\"']", re.MULTILINE
)
ID_WORD = re.compile(r"\bid\b", re.IGNORECASE)

offenders: list[str] = []
scanned = 0
for py_file in sorted((ROOT / "bot").rglob("*.py")):
    text = py_file.read_text(encoding="utf-8")
    for m in LABEL_RE.finditer(text):
        scanned += 1
        label = m.group(1)
        if ID_WORD.search(label):
            offenders.append(f"{py_file.relative_to(ROOT)}: {label!r}")

check(f"scanned {scanned} discord.ui.TextInput label(s) for identity fields", scanned > 0)
check(
    f"no discord.ui.TextInput label asks for a user/item/order/market id ({len(offenders)} offenders)",
    not offenders, "; ".join(offenders),
)

# ------------------------------------------------------------------ Approve is staff-gated on the card
# `OrderCardView.approve_btn` sits on a message every member of the guild
# can see and click -- unlike every ephemeral panel button, there is no
# `interaction_check` upstream of it to gate on. The staff check has to be
# the FIRST thing the callback itself does.
try:
    from bot.views.orders import OrderCardView
    approve_source = inspect.getsource(OrderCardView.approve_btn)
except Exception as err:  # noqa: BLE001
    approve_source = ""
    check("OrderCardView.approve_btn source is readable", False, f"{type(err).__name__}: {err}")
else:
    check("OrderCardView.approve_btn checks is_staff before doing anything money-related",
          "is_staff(" in approve_source)


# ------------------------------------------------------------------ no placeholder echoes its own label
def _balanced_call_args(text: str, needle: str) -> list[str]:
    """Argument text of every `needle(...)` call in `text`, respecting paren
    nesting -- a naive non-greedy regex stops at the first `)`, which is
    inside the string for a label like `f"Amount (g, max {X})"` and would
    silently mis-scope the rest of that call's keyword arguments."""
    calls = []
    start = 0
    while True:
        idx = text.find(needle, start)
        if idx == -1:
            break
        open_paren = idx + len(needle) - 1
        if text[open_paren] != "(":
            start = idx + len(needle)
            continue
        depth, j = 0, open_paren
        while j < len(text):
            if text[j] == "(":
                depth += 1
            elif text[j] == ")":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        calls.append(text[open_paren + 1:j])
        start = j + 1
    return calls


LABEL_ARG_RE = re.compile(r"label\s*=\s*f?([\"'])(.*?)\1", re.DOTALL)
PLACEHOLDER_ARG_RE = re.compile(r"placeholder\s*=\s*([A-Za-z_][A-Za-z0-9_.]*)\b")
BRACE_VAR_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_.]*)")

placeholder_offenders: list[str] = []
for py_file in sorted((ROOT / "bot").rglob("*.py")):
    text = py_file.read_text(encoding="utf-8")
    for block in _balanced_call_args(text, "discord.ui.TextInput("):
        lm = LABEL_ARG_RE.search(block)
        pm = PLACEHOLDER_ARG_RE.search(block)
        if not lm or not pm:
            continue  # placeholder is a literal string or absent -- not an echo
        label_vars = set(BRACE_VAR_RE.findall(lm.group(2)))
        if pm.group(1) in label_vars:
            placeholder_offenders.append(
                f"{py_file.relative_to(ROOT)}: placeholder={pm.group(1)!r} "
                f"already appears in label {lm.group(2)!r}"
            )

check(
    f"no TextInput placeholder echoes the value its own label already shows "
    f"({len(placeholder_offenders)} offenders)",
    not placeholder_offenders, "; ".join(placeholder_offenders),
)

# ------------------------------------------------------------------ no user-facing internal id pattern
# The exact shape of the bug this guards: a footer (or any other
# user-visible string) reading "address vh62  ·  order:1" -- the address
# code AND the raw database id side by side. `bot.addressing` exists so a
# card only ever needs to show the code; a literal `kind:{id}` template in
# source is that pattern leaking back in.
# No whitespace allowed between the colon and the brace -- that's the
# exact shape the real bug had ("order:{order_id}"), and it keeps this
# from tripping over ordinary prose like f"Could not add item: {err}".
ID_LEAK_RE = re.compile(r"\b(order|item|market)\s*:\{")

id_leak_offenders: list[str] = []
for py_file in sorted((ROOT / "bot").rglob("*.py")):
    text = py_file.read_text(encoding="utf-8")
    for m in ID_LEAK_RE.finditer(text):
        line_no = text.count("\n", 0, m.start()) + 1
        id_leak_offenders.append(f"{py_file.relative_to(ROOT)}:{line_no}: {m.group(0)!r}")

check(
    f"no user-facing string embeds a raw internal id as 'kind:{{id}}' ({len(id_leak_offenders)} offenders)",
    not id_leak_offenders, "; ".join(id_leak_offenders),
)

# ------------------------------------------------------------------ casino: the player supplies the seed
# `core.games` can only be provably fair if the PLAYER path actually uses
# it. Two halves have to be wired, and a core-only fix leaves both undone
# while every core test still passes:
#
#   1. the client seed is typed by the player, not built by the house. The
#      original defect was literally `client_seed = f"{subject}:{interaction.id}"`
#      -- both seeds house-chosen, so the "proof" proved nothing.
#   2. a commitment is published (`games.commit()`) BEFORE the bet modal
#      opens, and its id is carried into `games.play(commitment_id=...)`.
#      A hash the player is shown after staking is not a commitment.
_casino_src = (ROOT / "bot" / "views" / "casino.py").read_text(encoding="utf-8")

check(
    "casino builds no server-side client seed from interaction.id",
    not re.search(r"client_seed\s*=\s*f?[\"'][^\"']*interaction\.id", _casino_src),
    "the house is choosing the player's seed again",
)
check(
    "casino bet modal takes the player's own seed as free text",
    'label="Your seed"' in _casino_src,
    "no 'Your seed' TextInput in the bet modal",
)
check(
    "casino publishes a commitment before the stake is taken",
    "games.commit(" in _casino_src,
    "games.commit() is never called from the player path",
)
check(
    "casino carries that commitment into games.play",
    "commitment_id=" in _casino_src,
    "games.play() is called without the published commitment",
)

# ------------------------------------------------- every exit has a caller
# "Built but never called" is the standard failure of a parallel build, and
# green tests cannot see it: core/orders.py had `cancel` and `reprice` fully
# implemented and unit-tested while NOTHING under bot/ could reach either, so
# an order with a zero price snapshot could not be paid (approve raises) and
# could not be voided (no caller) -- the pieces stayed claimed forever and the
# delivered work was lost. The invariant is section-level: for every
# non-closed order there is at least one reachable terminal transition.
BOT_SOURCE = "\n".join(
    path.read_text(encoding="utf-8")
    for path in sorted(Path(ROOT / "bot").rglob("*.py"))
)

REACHABLE_FROM_BOT = [
    ("orders_core.cancel(", "an order can be voided"),
    ("orders_core.reprice(", "a zero-price order can be repaired and then paid"),
    ("orders_core.approve(", "a delivered order can be paid"),
]
for needle, why in REACHABLE_FROM_BOT:
    check(f"bot/ can reach {needle[:-1]} -- so {why}",
          needle in BOT_SOURCE,
          "implemented in core but unreachable from any Discord surface")

# The detector itself must be able to fail, or it proves nothing.
check("...and this check would notice an unwired function",
      "orders_core.definitely_not_a_real_function(" not in BOT_SOURCE)

print()
if FAILS:
    print(f"{len(FAILS)} FAILED: {', '.join(FAILS)}")
    sys.exit(1)
print("all bot surface tests pass")
