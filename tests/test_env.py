"""`.env` loading.

Same shape as its neighbours: a script, run in its own interpreter by
`run_tests.py`. Prints one line per check and exits non-zero if any failed.

The check that matters here is the NEGATIVE one -- a value already present
in the real environment must survive the file. Wispbyte injects some
variables itself, and a loader that overrode them would leave a panel field
and a file disagreeing, with nothing on screen saying which one won.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.env import load_env_file  # noqa: E402

FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if ok else 'FAIL'}  {name}{'  ' + detail if detail and not ok else ''}")
    if not ok:
        FAILS.append(name)


def write(body: str) -> Path:
    path = Path(tempfile.mkdtemp()) / ".env"
    path.write_text(body, encoding="utf-8")
    return path


print("loading a .env file")

for name in ("NOLA_T_PLAIN", "NOLA_T_Q", "NOLA_T_SQ", "NOLA_T_EX", "NOLA_T_EQ", "NOLA_T_OK"):
    os.environ.pop(name, None)

path = write(
    "# a comment\n"
    "\n"
    "NOLA_T_PLAIN=hello\n"
    'NOLA_T_Q="wrapped"\n'
    "NOLA_T_SQ='single'\n"
    "export NOLA_T_EX=viaexport\n"
    "NOLA_T_EQ=a=b=c\n"
    "this line is not an assignment\n"
    "NOLA_T_OK=fine\n"
)
loaded = load_env_file(path, quiet=True)

check("plain KEY=VALUE is loaded", os.environ.get("NOLA_T_PLAIN") == "hello")
check("double quotes are stripped", os.environ.get("NOLA_T_Q") == "wrapped")
check("single quotes are stripped", os.environ.get("NOLA_T_SQ") == "single")
check("a leading `export ` is tolerated", os.environ.get("NOLA_T_EX") == "viaexport")
check("a value containing = is not split twice", os.environ.get("NOLA_T_EQ") == "a=b=c")
check("a malformed line is skipped, not raised", os.environ.get("NOLA_T_OK") == "fine")
check("comments and blank lines are not counted", loaded == 6, f"loaded={loaded}")

print("\nprecedence: the real environment always wins")

os.environ["NOLA_T_SET"] = "from_real_env"
load_env_file(write("NOLA_T_SET=from_file\n"), quiet=True)
check("a variable already set is NOT overridden by the file",
      os.environ.get("NOLA_T_SET") == "from_real_env",
      f"got {os.environ.get('NOLA_T_SET')!r}")

os.environ["NOLA_T_BLANK"] = ""
load_env_file(write("NOLA_T_BLANK=filled\n"), quiet=True)
check("an EMPTY environment value counts as unset, so the file may fill it",
      os.environ.get("NOLA_T_BLANK") == "filled",
      "a panel field left blank arrives as an empty string")

print("\ndegenerate cases")

check("a missing file is not an error",
      load_env_file(Path(tempfile.mkdtemp()) / "absent.env", quiet=True) == 0)

print("\nthe entrypoints load .env ABOVE their core imports")

for entrypoint in ("run_shop.py", "run_web.py"):
    text = (ROOT / entrypoint).read_text(encoding="utf-8")
    if "load_env_file()" not in text:
        check(f"{entrypoint} calls load_env_file()", False)
        continue
    at = text.index("load_env_file()")
    late = [m for m in ("from core.config", "from core.db", "from bot.main", "from web.server")
            if m in text and text.index(m) < at]
    check(f"{entrypoint} loads .env before importing core", not late,
          f"imported first: {', '.join(late)} -- core.db resolves NOLA_DB_PATH at import time")

print()
if FAILS:
    print(f"{len(FAILS)} FAILED: {', '.join(FAILS)}")
    sys.exit(1)
print("all env tests pass")
