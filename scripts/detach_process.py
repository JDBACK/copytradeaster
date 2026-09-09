#!/usr/bin/env python3
"""Start one long-running command in a new session and record its PID.

This small launcher is used by ``dev-up.sh`` instead of shell ``nohup``.
Codex/PTY runners clean up descendants that remain in the caller's process
group; ``start_new_session=True`` gives the bot and cloudflared their own
session so they survive after the startup command exits.
"""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pid-file", required=True, type=Path)
    parser.add_argument("--log-file", required=True, type=Path)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.command and args.command[0] == "--":
        args.command = args.command[1:]
    if not args.command:
        parser.error("a command is required after --")

    args.pid_file.parent.mkdir(parents=True, exist_ok=True)
    args.log_file.parent.mkdir(parents=True, exist_ok=True)
    with args.log_file.open("ab", buffering=0) as log:
        process = subprocess.Popen(
            args.command,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            close_fds=True,
            start_new_session=True,
        )
    args.pid_file.write_text(f"{process.pid}\n", encoding="ascii")
    print(process.pid)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
