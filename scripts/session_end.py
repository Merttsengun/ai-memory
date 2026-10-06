#!/usr/bin/env python3
"""SessionEnd / PreCompact (Claude Code): queue a summary job and start the summarizer
independently of the terminal.

Write the job file (state/hookin-*.json) to disk before starting the summarizer;
if the summarizer dies, the file remains and sweep_stale retries it at the next
session start. Failed summarizer runs also keep the file (summarize.py).
"""

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


def update_index(project_id: str, project_dir: str) -> None:
    """Readable path -> identity log; no script reads it as a source of truth."""
    index_path = PROJECTS_ROOT / "index.json"
    from locks import held
    with held(PROJECTS_ROOT / "_index.lock", stale_seconds=60, wait_seconds=5) as got:
        if got:
            _update_index_locked(index_path, project_id, project_dir)


def _update_index_locked(index_path: Path, project_id: str, project_dir: str) -> None:
    try:
        data = json.loads(index_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            data = {}
    except (OSError, ValueError):
        data = {}
    data[project_id] = project_dir
    tmp = index_path.with_name(f"index.json.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, index_path)


def main() -> int:
    if os.environ.get(INTERNAL_ENV):
        return 0
    parser = argparse.ArgumentParser()
    parser.add_argument("--reason", choices=("sessionend", "precompact"), required=True)
    args = parser.parse_args()
    try:
        utf8_stdio(stdin=True)
        raw = sys.stdin.read()
        hook = json.loads(raw) if raw.strip() else {}
    except (OSError, ValueError):
        return 0
    project_dir = os.environ.get("CLAUDE_PROJECT_DIR") or hook.get("cwd")
    if not isinstance(project_dir, str) or not project_dir or not isinstance(hook, dict):
        return 0
    if is_excluded(project_dir):
        return 0  # left out of the memory system on purpose (config exclude_projects)

    import project_id as pid_mod
    from bg import spawn_detached

    project_id = pid_mod.project_id(project_dir)
    project_data = PROJECTS_ROOT / project_id
    state_dir = project_data / "state"
    state_dir.mkdir(parents=True, exist_ok=True)

    if os.name != "nt":
        os.umask(0o077)  # The job file contains the transcript path.
    job = state_dir / f"hookin-{uuid.uuid4().hex[:12]}.json"
    tmp = job.with_name(f".{job.name}.tmp")
    tmp.write_text(json.dumps({**hook, "_reason": args.reason}, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, job)

    if args.reason == "sessionend":
        try:
            update_index(project_id, project_dir)
        except OSError:
            pass

    spawn_detached(
        [sys.executable, str(SCRIPT_DIR / "summarize.py"), "--hook-input", str(job),
         "--memory-dir", str(project_data), "--reason", args.reason],
        state_dir / f"lastrun-{args.reason}.log",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
