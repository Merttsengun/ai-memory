#!/usr/bin/env python3
"""Codex SessionEnd/PreCompact: hand a small job file to the background summarizer."""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
from config import INTERNAL_ENV, PROJECTS_ROOT, is_excluded, utf8_stdio  # noqa: E402
from project_id import project_id  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reason", choices=("sessionend", "precompact"), required=True)
    args = parser.parse_args()
    if os.environ.get(INTERNAL_ENV):
        return 0
    try:
        utf8_stdio(stdin=True)
        hook_input = json.load(sys.stdin)
        cwd = hook_input.get("cwd")
        session_id = hook_input.get("session_id")
        transcript_path = hook_input.get("transcript_path")
        if not all(isinstance(value, str) and value for value in (cwd, session_id, transcript_path)):
            return 0
        # Codex run as Claude's sub-agent: its result is in the main Claude session already.
        from codex_common import is_claude_subagent
        if is_claude_subagent() or is_excluded(cwd):
            return 0

        pid = project_id(cwd)
        state = PROJECTS_ROOT / pid / "state"
        try:  # readable project name for the vault pages (Claude's SessionEnd does the same)
            from session_end import update_index
            update_index(pid, cwd)
        except OSError:
            pass
        state.mkdir(parents=True, exist_ok=True)
        pending = state / f"codex-hookin-{uuid.uuid4().hex}.json"
        payload = {"cwd": cwd, "session_id": session_id, "transcript_path": transcript_path, "reason": args.reason}
        tmp = pending.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, pending)
        # Detached (like the Claude side): closing the terminal / editor must not
        # kill the summarizer. If it dies anyway, the job file stays for the sweep.
        from bg import spawn_detached
        spawn_detached([sys.executable, str(SCRIPT_DIR / "codex_summarize.py"), "--hook-input", str(pending)],
                       state / "lastrun-codex.log")
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
