#!/usr/bin/env python3
"""Bot process entrypoint. This is what `run_all.py` execs as the "bot"
child, and it is also usable standalone for local development."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bot.main import main  # noqa: E402

if __name__ == "__main__":
    main()
