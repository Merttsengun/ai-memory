#!/usr/bin/env python3
"""Codex SessionStart: inject the same per-project context the Claude hook does
(the block itself is built by scripts/session_start.py:build_context)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
from config import INTERNAL_ENV, PROJECTS_ROOT, is_excluded, utf8_stdio  # noqa: E402
from project_id import project_id  # noqa: E402

STALE_SECONDS = 300


def recover_stale(project_data: Path) -> None:
    state = project_data / "state"
    if not state.is_dir():
        return
    now = time.time()
    from codex_common import requeue_orphans
    requeue_orphans(state, now)
    # At most ONE summarizer per session start (the oldest job): after a Codex update many
    # jobs may be waiting, and starting them all at once would burn the quota. The sweep
    # takes the rest, codex_jobs_per_sweep at a time.
    try:
        jobs = sorted(state.glob("codex-hookin-*.json"), key=lambda p: p.stat().st_mtime)
    except OSError:  # a job finished (renamed) while sorting: the next start retries
        return
    for pending in jobs:
        try:
            if now - pending.stat().st_mtime < STALE_SECONDS:
                continue
            try:  # after a transient error (limit etc.) the job waits until _not_before
                if float(json.loads(pending.read_text(encoding="utf-8")).get("_not_before") or 0) > now:
                    continue
            except (ValueError, AttributeError):
                pass
            subprocess.Popen(
                [sys.executable, str(SCRIPT_DIR / "codex_summarize.py"), "--hook-input", str(pending)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            return
        except OSError:
            continue


def main() -> int:
    if os.environ.get(INTERNAL_ENV):
        return 0
    try:
        utf8_stdio(stdin=True, stdout=True)
        hook_input = json.load(sys.stdin)
        cwd = hook_input.get("cwd")
        if not isinstance(cwd, str) or not cwd:
            return 0
        # Codex started from inside Claude Code (a sub-agent run): no memory, so the
        # review stays independent and spends no tokens on it.
        from codex_common import is_claude_subagent
        if is_claude_subagent() or is_excluded(cwd):
            return 0
        try:
            from merge_projects import auto as fold_legacy
            fold_legacy(cwd)
        except (OSError, ValueError):
            pass
        project_data = PROJECTS_ROOT / project_id(cwd)
        (project_data / "state").mkdir(parents=True, exist_ok=True)
        from session_start import _ensure_project_page
        _ensure_project_page(project_data, cwd)
        recover_stale(project_data)
        from session_start import build_context
        context = build_context(project_data, "codex")
        import usage  # 7-day token tracking: how much context the memory adds to every session
        usage.record("inject-codex", project_data.name, usage.estimate_tokens(len(context)), chars=len(context))
        output = {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": context}}
        print(json.dumps(output, ensure_ascii=False))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
