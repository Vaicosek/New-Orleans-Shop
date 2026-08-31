"""Which port the website binds, and where that number came from.

This exists because getting it wrong is SILENT. Binding the wrong port
succeeds -- the process starts clean and every request from the panel lands
on a port with nothing listening. On a host with no shell there is nothing
to inspect afterwards, so the resolution order is pinned here and the choice
is printed at startup.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Point the DB somewhere disposable before run_web imports core.db, which
# resolves NOLA_DB_PATH at import time.
import tempfile  # noqa: E402

os.environ["NOLA_DB_PATH"] = str(Path(tempfile.mkdtemp()) / "test.db")

import run_web  # noqa: E402

FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if ok else 'FAIL'}  {name}{'  ' + detail if detail and not ok else ''}")
    if not ok:
        FAILS.append(name)


def resolve_with(**env) -> tuple[int, str]:
    for name in run_web.PORT_SOURCES:
        os.environ.pop(name, None)
    for key, value in env.items():
        os.environ[key] = str(value)
    return run_web.resolve_port()


print("port resolution order: SERVER_PORT, then PORT, then NOLA_WEB_PORT, then the default")

check("SERVER_PORT alone is used (Pterodactyl/Wispbyte allocation)",
      resolve_with(SERVER_PORT=25580) == (25580, "SERVER_PORT"))

check("SERVER_PORT BEATS NOLA_WEB_PORT -- the panel allocation wins over our preference",
      resolve_with(SERVER_PORT=25580, NOLA_WEB_PORT=8080) == (25580, "SERVER_PORT"),
      "this is the whole bug: the panel routes to its allocation, not to 8080")

check("SERVER_PORT beats PORT",
      resolve_with(SERVER_PORT=25580, PORT=3000) == (25580, "SERVER_PORT"))

check("PORT is used when SERVER_PORT is absent",
      resolve_with(PORT=3000, NOLA_WEB_PORT=8080) == (3000, "PORT"))

check("NOLA_WEB_PORT is used when no host variable is set (local dev)",
      resolve_with(NOLA_WEB_PORT=9001) == (9001, "NOLA_WEB_PORT"))

check("the default is reported AS the default, not as a configured value",
      resolve_with() == (8080, "default"),
      "'8080 (default)' and '8080 (NOLA_WEB_PORT)' are different situations")

print("\nan empty panel field must not be mistaken for a configured port")

check("SERVER_PORT set to an empty string falls through instead of crashing",
      resolve_with(SERVER_PORT="", NOLA_WEB_PORT=9002) == (9002, "NOLA_WEB_PORT"),
      "a blank panel field arrives as an empty string, not as unset")

print("\nthe website binds all interfaces, never SERVER_IP")

source = (ROOT / "run_web.py").read_text(encoding="utf-8")
check("run_app binds 0.0.0.0 explicitly",
      'host="0.0.0.0"' in source,
      "SERVER_IP can be the allocation's public address, which is not on a local interface")
check("SERVER_IP is not used as a bind address", 'env_str("SERVER_IP"' not in source)

print()
if FAILS:
    print(f"{len(FAILS)} FAILED: {', '.join(FAILS)}")
    sys.exit(1)
print("all web port tests pass")
