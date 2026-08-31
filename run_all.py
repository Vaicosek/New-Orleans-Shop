#!/usr/bin/env python3
"""Process supervisor: bot + web as children.

This is the one operational thing AbexTech's `Restocker_main.py` does not
have -- it runs directly under the host with nothing watching it, so a
crash is a silent outage until someone notices Discord went quiet.

What this does, per CONTRACT.md section 12:
  - prefixed, unbuffered child output (`python -u`, one `[label] ...` line
    per line of child output, printed as it arrives)
  - exponential backoff between restarts
  - gives up on a child after too many RAPID failures instead of
    restart-storming a host that has no shell to intervene from
  - forwards SIGTERM to every child so a Wispbyte panel Stop closes the
    Discord gateway connection cleanly instead of killing it mid-write

This is the Wispbyte "APP PY FILE". `run_web.py` is optional -- if it does
not exist yet, that child is skipped with one line, not treated as a
failure.
"""
from __future__ import annotations

import asyncio
import signal
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PY = sys.executable

RAPID_FAILURE_SECONDS = 30.0   # an exit sooner than this counts as "rapid"
MAX_RAPID_FAILURES = 5         # consecutive rapid failures before giving up
BASE_BACKOFF = 2.0
MAX_BACKOFF = 60.0


class Child:
    def __init__(self, label: str, script: Path):
        self.label = label
        self.script = script
        self.proc: asyncio.subprocess.Process | None = None
        self.rapid_failures = 0
        self.gave_up = False

    def backoff_seconds(self) -> float:
        return min(BASE_BACKOFF * (2 ** max(0, self.rapid_failures - 1)), MAX_BACKOFF)


async def _pump(stream: asyncio.StreamReader | None, label: str) -> None:
    if stream is None:
        return
    while True:
        line = await stream.readline()
        if not line:
            return
        text = line.decode(errors="replace").rstrip("\n")
        print(f"[{label}] {text}", flush=True)


async def run_child(child: Child, stop_event: asyncio.Event) -> None:
    if not child.script.exists():
        print(f"[supervisor] {child.label}: {child.script.name} not found -- skipping", flush=True)
        return

    while not stop_event.is_set() and not child.gave_up:
        started = time.monotonic()
        print(f"[supervisor] starting {child.label} ({child.script.name})", flush=True)
        proc = await asyncio.create_subprocess_exec(
            PY, "-u", str(child.script),
            cwd=str(ROOT),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        child.proc = proc
        pumps = [
            asyncio.create_task(_pump(proc.stdout, child.label)),
            asyncio.create_task(_pump(proc.stderr, child.label)),
        ]
        returncode = await proc.wait()
        for p in pumps:
            await p
        child.proc = None
        alive_for = time.monotonic() - started

        if stop_event.is_set():
            print(f"[supervisor] {child.label} stopped (exit {returncode})", flush=True)
            return

        if alive_for < RAPID_FAILURE_SECONDS:
            child.rapid_failures += 1
        else:
            child.rapid_failures = 0

        print(
            f"[supervisor] {child.label} exited (code {returncode}) after {alive_for:.1f}s "
            f"-- rapid failures {child.rapid_failures}/{MAX_RAPID_FAILURES}",
            flush=True,
        )

        if child.rapid_failures >= MAX_RAPID_FAILURES:
            child.gave_up = True
            print(
                f"[supervisor] {child.label} failed {MAX_RAPID_FAILURES} times rapidly -- "
                f"giving up rather than restart-storming. Fix it and restart the panel.",
                flush=True,
            )
            return

        delay = child.backoff_seconds()
        print(f"[supervisor] restarting {child.label} in {delay:.1f}s", flush=True)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=delay)
        except asyncio.TimeoutError:
            pass


async def main_async() -> int:
    children = [
        Child("bot", ROOT / "run_shop.py"),
        Child("web", ROOT / "run_web.py"),
    ]
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _on_signal(sig_name: str) -> None:
        print(f"[supervisor] received {sig_name} -- stopping children", flush=True)
        stop_event.set()
        for c in children:
            if c.proc is not None and c.proc.returncode is None:
                try:
                    c.proc.send_signal(signal.SIGTERM)
                except ProcessLookupError:
                    pass

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _on_signal, sig.name)
        except NotImplementedError:
            pass  # some platforms/event loops don't support this; SIGINT still works via default

    tasks = [asyncio.create_task(run_child(c, stop_event)) for c in children]
    await asyncio.gather(*tasks)

    if stop_event.is_set():
        return 0
    if all(c.gave_up or not c.script.exists() for c in children):
        print("[supervisor] every configured child gave up or is missing -- exiting", flush=True)
        return 1
    return 0


def main() -> None:
    try:
        code = asyncio.run(main_async())
    except KeyboardInterrupt:
        code = 0
    sys.exit(code)


if __name__ == "__main__":
    main()
