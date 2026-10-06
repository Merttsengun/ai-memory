#!/usr/bin/env python3
"""Start background work independent of the hook and the terminal.

Why: a summarizer started with `nohup ... &` was killed together with Claude
Code / the terminal, so part of the long sessions were never summarized. On
Windows: a new process group + breakaway from the job object (without it if
not allowed); on POSIX: a new session. Output goes to a log file: if the
hook's stdout stays open, Claude Code waits for the hook until its timeout.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

_DETACHED_PROCESS = 0x00000008
_CREATE_NEW_PROCESS_GROUP = 0x00000200
_CREATE_BREAKAWAY_FROM_JOB = 0x01000000
_CREATE_NO_WINDOW = 0x08000000


def spawn_detached(args: list[str], log_path: Path) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "ab") as log:
        common = dict(stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, close_fds=True)
        if os.name != "nt":
            subprocess.Popen(args, start_new_session=True, **common)
            return
        # Not DETACHED_PROCESS: it overrides CREATE_NO_WINDOW and leaves the process
        # without a console, so every child (claude -p, git) opens a visible window.
        flags = _CREATE_NEW_PROCESS_GROUP | _CREATE_NO_WINDOW
        try:
            subprocess.Popen(args, creationflags=flags | _CREATE_BREAKAWAY_FROM_JOB, **common)
        except OSError:
            # The parent job object does not allow breakaway: start detached anyway.
            subprocess.Popen(args, creationflags=flags, **common)
