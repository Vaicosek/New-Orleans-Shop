"""bot/ surface test.

Imports every module under `bot/` with a stubbed `discord` (no real
discord.py, no network, no token -- see `tests/_stubs/discord_stub.py`,
modelled on AbexTech's own `_harness/stubs.py`) and statically asserts the
rules from CONTRACT.md section 7 that matter most and are cheap to check
without a live gateway connection:

  - exactly SEVEN top-level slash commands exist, across every cog
  - no `discord.ui.TextInput` anywhere carries a label asking for a user,
    item, order or market id -- "never a text field for an identity"
  - every slash command handler's FIRST statement defers the interaction --
    the 3-second window rule
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

# ------------------------------------------------------------------ exactly 7 slash commands
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
check(
    f"exactly 7 top-level slash commands exist (found {len(commands_found)}: {command_names})",
    len(commands_found) == 7,
)
check("all 7 command names are distinct",
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

print()
if FAILS:
    print(f"{len(FAILS)} FAILED: {', '.join(FAILS)}")
    sys.exit(1)
print("all bot surface tests pass")
