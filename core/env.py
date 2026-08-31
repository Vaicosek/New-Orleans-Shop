"""Read `.env` into the process environment, before anything reads config.

Wispbyte's panel has no shell, and its Python egg exposes a fixed set of
variables -- there is no field to put DISCORD_TOKEN in. What the panel does
have is a file manager. So the only way to configure this deployment by
clicking is a file the process reads for itself at startup, which is what
this module is. No dependency: `python-dotenv` would be a third package in
`requirements.txt` to parse forty lines of `KEY=VALUE`.

**Precedence: a variable already present in the real environment always
wins.** The file fills gaps; it never overrides the host. A value injected
by the panel, or exported in a shell for a local run, is therefore never
silently shadowed by a stale line in a file nobody remembered was there --
and that shadowing is the failure mode that makes dotenv loaders infuriating
to debug, because the file is invisible from the place you are looking.

**This must run before `core.config`, `core.db` or `core.games` are
imported.** `core.db` resolves NOLA_DB_PATH at module import time, so a load
that happens after that import is read too late to have any effect. That is
why every entrypoint calls this as its first statement instead of relying on
import order to work out -- import order is exactly the kind of thing that
holds until someone adds an import above yours.

`.env` is gitignored and must stay that way: CONTRACT.md section 11 -- no
token, webhook, secret or access-granting id is ever committed to this repo.
"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ENV_PATH = ROOT / ".env"


def _unquote(value: str) -> str:
    """Strip ONE layer of matching quotes. A token pasted out of a panel
    field often arrives wrapped in them, and `"abc"` is not the same string
    as `abc` when Discord checks it."""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def load_env_file(path: Path | None = None, *, quiet: bool = False) -> int:
    """Load `path` (default: `.env` beside this project) into os.environ.

    Returns the number of variables actually set. Never raises: a missing
    file is the normal case for a machine configured some other way, and a
    single malformed line should not be the reason a shop is offline. A
    malformed line is reported by number and skipped; anything genuinely
    required is caught immediately afterwards by `core.config`, which fails
    loudly and names the variable.
    """
    path = path or DEFAULT_ENV_PATH
    if not path.is_file():
        if not quiet:
            print(f"env: no {path.name} at {path} -- using the real environment only", flush=True)
        return 0

    loaded = 0
    skipped: list[int] = []
    shadowed: list[str] = []

    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not key:
            skipped.append(number)
            continue
        if key in os.environ and os.environ[key] != "":
            shadowed.append(key)
            continue
        os.environ[key] = _unquote(value.strip())
        loaded += 1

    if not quiet:
        print(f"env: loaded {loaded} variable(s) from {path}", flush=True)
        if shadowed:
            # Loud on purpose. "I set it in the file and nothing changed" is
            # otherwise an unfindable half-hour.
            print(
                f"env: {len(shadowed)} already set in the environment, file value IGNORED: "
                f"{', '.join(sorted(shadowed))}",
                flush=True,
            )
        if skipped:
            print(
                f"env: {path.name} line(s) {', '.join(str(n) for n in skipped)} are not KEY=VALUE -- skipped",
                flush=True,
            )
    return loaded
