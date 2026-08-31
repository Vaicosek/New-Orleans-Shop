#!/usr/bin/env python3
"""Run the test suite: one file, one interpreter.

**Do not run this suite with `pytest`.** Every file here is a script that
performs its checks at import and exits non-zero on failure, and that shape
is deliberate -- several of the checks are only meaningful in a fresh
interpreter:

  - `test_no_wagering_on_web.py` asserts that nothing reachable from `web/`
    has pulled `core.games` or `core.predictions` into `sys.modules`. Any
    betting test running earlier in the same process imports them, and the
    guard then refuses to report a pass it cannot stand behind.
  - the DB-backed files each point `NOLA_DB_PATH` at their own temporary
    database before importing `core.db` -- which resolves that path once, at
    import. In a shared process the first file to import wins and every
    later file silently runs against its database.

`pytest` imports all of them into one process, so it hits both problems at
once: an INTERNALERROR from the first, foreign-key failures from the second.
Neither is a bug in the tests. The runner is the thing that has to be right,
and this is it.

    python run_tests.py            # everything
    python run_tests.py money      # only files whose name contains "money"
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TESTS = ROOT / "tests"


def main() -> int:
    needle = sys.argv[1] if len(sys.argv) > 1 else ""
    files = sorted(p for p in TESTS.glob("test_*.py") if needle in p.name)
    if not files:
        print(f"no test files match {needle!r} in {TESTS}")
        return 1

    failed: list[str] = []
    width = max(len(p.name) for p in files)
    print(f"running {len(files)} test file(s), one interpreter each\n")

    for path in files:
        started = time.monotonic()
        result = subprocess.run(
            [sys.executable, str(path)],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
        )
        elapsed = time.monotonic() - started
        ok = result.returncode == 0
        print(f"  {'ok  ' if ok else 'FAIL'}  {path.name:<{width}}  {elapsed:5.2f}s")
        if not ok:
            failed.append(path.name)
            # The whole point of a failing run is reading why, so print it
            # here rather than making someone re-run the file by hand.
            body = (result.stdout + result.stderr).rstrip()
            print("\n".join(f"        {line}" for line in body.splitlines()[-40:]))
            print()

    print()
    if failed:
        print(f"{len(failed)} of {len(files)} FAILED: {', '.join(failed)}")
        return 1
    print(f"all {len(files)} test files pass")
    return 0


if __name__ == "__main__":
    sys.exit(main())
