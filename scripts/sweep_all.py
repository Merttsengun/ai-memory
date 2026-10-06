#!/usr/bin/env python3
"""Scheduled sweep (Windows Task Scheduler, every 30 minutes).

Why: recovery used to run only when the SAME project was reopened. Sessions
killed when the window closed, waiting on limits, or missed by hooks remained
unsummarized until the project was opened again (sometimes weeks later).

Each run, within a limited budget:
  Claude: identify projects from ~/.claude/projects/<name>/ folders, use
          sweep_stale.run to queue missed sessions and process pending jobs.
  Codex:  queue finished, unsummarized sessions under ~/.codex/sessions,
          process pending codex-hookin jobs across all projects.
Only one sweep runs at a time (_sweep.lock). Write one result line to _sweep.log.

Run manually: py -3 sweep_all.py [--dry-run]
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
from config import INTERNAL_ENV, PROJECTS_ROOT, is_excluded, load_config  # noqa: E402
CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"
CODEX_SESSIONS = Path.home() / ".codex" / "sessions"

MAX_AGE_DAYS = 14
MIN_IDLE_SECONDS = 30 * 60          # Leave sessions that may still be open alone.
CODEX_ENQUEUE_PER_RUN = 4
CODEX_STALE_SECONDS = 300
LOCK_STALE_SECONDS = 2 * 3600
CWD_RE = re.compile(r'"cwd"\s*:\s*"((?:[^"\\]|\\.)*)"')
TEMP_ROOT = Path(tempfile.gettempdir()).resolve()


def log_line(text: str) -> None:
    path = PROJECTS_ROOT / "_sweep.log"
    try:
        if path.exists() and path.stat().st_size > 256 * 1024:
            path.replace(path.with_suffix(".log.1"))
        with path.open("a", encoding="utf-8") as fh:
            fh.write(f"{dt.datetime.now().isoformat(timespec='seconds')} {text}\n")
    except OSError:
        pass


def usable_cwd(raw: str | None) -> str | None:
    """An existing, non-temporary project folder (otherwise None:
    avoid creating empty memory folders for deleted/moved projects)."""
    if not raw:
        return None
    try:
        path = Path(raw).resolve()
    except OSError:
        return None
    if not path.is_dir():
        return None
    if path == TEMP_ROOT or TEMP_ROOT in path.parents:
        return None
    return str(path)


def transcript_cwd(transcript: Path) -> str | None:
    try:
        with transcript.open("r", encoding="utf-8", errors="ignore") as fh:
            head = fh.read(64 * 1024)
    except OSError:
        return None
    match = CWD_RE.search(head)
    if not match:
        return None
    try:
        return json.loads(f'"{match.group(1)}"')
    except ValueError:
        return None


# --------------------------------------------------------------------- Claude
def sweep_claude(dry_run: bool) -> tuple[int, int]:
    import project_id as pid_mod
    import sweep_stale

    now = time.time()
    projects = 0
    # 1) Queue missed sessions across all projects (cheap, no model calls).
    for tdir in sorted(CLAUDE_PROJECTS.glob("*")):
        if not tdir.is_dir():
            continue
        recent = [p for p in tdir.glob("*.jsonl") if now - p.stat().st_mtime < MAX_AGE_DAYS * 86400]
        if not recent:
            continue
        cwd = usable_cwd(transcript_cwd(max(recent, key=lambda p: p.stat().st_mtime)))
        if cwd is None or is_excluded(cwd):
            continue
        memory_dir = PROJECTS_ROOT / pid_mod.project_id(cwd)
        projects += 1
        if dry_run:
            continue
        (memory_dir / "state").mkdir(parents=True, exist_ok=True)
        try:
            sweep_stale.enqueue_missed(memory_dir, tdir, "", now)
        except OSError:
            continue
    if dry_run:
        return projects, 0
    # 2) Spend the budget fairly: order projects by their oldest pending job
    #    (alphabetical order must not starve projects at the end on every run).
    oldest: list[tuple[float, Path]] = []
    for memory_dir in PROJECTS_ROOT.glob("*"):
        if memory_dir.name.startswith("_") or is_excluded(project_id=memory_dir.name):
            continue
        jobs = [p.stat().st_mtime for p in (memory_dir / "state").glob("hookin-*.json")]
        if jobs:
            oldest.append((min(jobs), memory_dir))
    processed = 0
    order = [memory_dir for _, memory_dir in sorted(oldest)]
    while processed < load_config()["claude_jobs_per_sweep"] and order:
        progress = 0
        for memory_dir in list(order):  # One job per project in turn
            if processed >= load_config()["claude_jobs_per_sweep"]:
                break
            done = sweep_stale.run(memory_dir, None, "", 1)
            if not done:
                order.remove(memory_dir)  # No ready jobs left to process
            processed += done
            progress += done
        if not progress:
            break
    return projects, processed


# ---------------------------------------------------------------------- Codex
def _codex_meta(transcript: Path) -> dict | None:
    try:
        with transcript.open("r", encoding="utf-8", errors="ignore") as fh:
            first = json.loads(fh.readline() or "{}")
    except (OSError, ValueError):
        return None
    if not isinstance(first, dict) or first.get("type") != "session_meta":
        return None
    meta = first.get("payload")
    return meta if isinstance(meta, dict) else None


def _codex_done_mtimes(state: Path) -> dict[str, float]:
    done: dict[str, float] = {}
    try:
        for line in (state / "codex-done.txt").read_text(encoding="utf-8").splitlines():
            parts = line.split("\t")
            if len(parts) >= 3 and parts[2].isdigit():
                done[parts[0]] = max(done.get(parts[0], 0.0), float(parts[2]))
    except OSError:
        pass
    return done


def _codex_pending_sessions(state: Path) -> set[str]:
    ids: set[str] = set()
    for path in list(state.glob("codex-hookin-*")) + list((state / "failed").glob("codex-hookin-*")):
        try:
            sid = json.loads(path.read_text(encoding="utf-8")).get("session_id")
        except (OSError, ValueError, AttributeError):
            continue
        if isinstance(sid, str):
            ids.add(sid)
    return ids


def enqueue_codex_missed(dry_run: bool) -> int:
    """Queue Codex sessions missed by hooks or only partially summarized.
    Skip `codex exec` sessions Claude launched as subagents (originator
    codex_exec). Known limitation: manually launched exec sessions with missed
    hooks are not recovered here either."""
    import project_id as pid_mod

    now = time.time()
    added = 0
    files = sorted(CODEX_SESSIONS.glob("*/*/*/rollout-*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    for transcript in files:
        if added >= CODEX_ENQUEUE_PER_RUN:
            break
        age = now - transcript.stat().st_mtime
        if age > MAX_AGE_DAYS * 86400:
            break  # Sorted by mtime: the rest are older.
        if age < MIN_IDLE_SECONDS:
            continue
        meta = _codex_meta(transcript)
        if not meta or meta.get("originator") == "codex_exec":
            continue
        sid, cwd = meta.get("id"), usable_cwd(meta.get("cwd"))
        if not isinstance(sid, str) or cwd is None or is_excluded(cwd):
            continue
        state = PROJECTS_ROOT / pid_mod.project_id(cwd) / "state"
        if transcript.stat().st_mtime <= _codex_done_mtimes(state).get(sid, -1.0) + 1:
            continue
        if sid in _codex_pending_sessions(state):
            continue
        if dry_run:
            added += 1
            continue
        state.mkdir(parents=True, exist_ok=True)
        job = state / f"codex-hookin-missed-{re.sub(r'[^a-zA-Z0-9-]', '', sid)[:12]}.json"
        payload = {"cwd": cwd, "session_id": sid, "transcript_path": str(transcript), "reason": "recovered"}
        try:
            with job.open("x", encoding="utf-8") as fh:
                fh.write(json.dumps(payload, ensure_ascii=False))
        except FileExistsError:
            continue
        old = now - CODEX_STALE_SECONDS - 1
        os.utime(job, (old, old))
        added += 1
    return added


def process_codex_jobs(dry_run: bool) -> int:
    if load_config()["pause_summaries"]:
        return 0  # paused: jobs keep waiting, no model calls
    now = time.time()
    ready: list[tuple[float, Path]] = []
    for state in sorted(PROJECTS_ROOT.glob("*/state")):
        if is_excluded(project_id=state.parent.name):
            continue
        # Orphaned job of a dead summarizer: requeue after 1 hour.
        for orphan in state.glob("codex-hookin-*.json.running-*"):
            try:
                if now - orphan.stat().st_mtime > 3600:
                    original = orphan.with_name(orphan.name.split(".running-")[0])
                    if not original.exists() and not dry_run:
                        orphan.replace(original)
            except OSError:
                continue
        for job in state.glob("codex-hookin-*.json"):
            try:
                mtime = job.stat().st_mtime
                if now - mtime < CODEX_STALE_SECONDS:
                    continue
                not_before = json.loads(job.read_text(encoding="utf-8")).get("_not_before") or 0
                if float(not_before) > now:
                    continue
            except (OSError, ValueError, AttributeError):
                continue
            ready.append((mtime, job))
    # Fair order: round-robin across projects, oldest job first within each project.
    by_project: dict[Path, list[Path]] = {}
    for _, job in sorted(ready):
        by_project.setdefault(job.parent, []).append(job)
    order: list[Path] = []
    while any(by_project.values()):
        for queue in by_project.values():
            if queue:
                order.append(queue.pop(0))
    processed = 0
    for job in order[:load_config()["codex_jobs_per_sweep"]]:
        if dry_run:
            processed += 1
            continue
        try:
            subprocess.run([sys.executable, str(SCRIPT_DIR / "codex_summarize.py"),
                            "--hook-input", str(job)],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=420, check=False)
        except (OSError, subprocess.TimeoutExpired):
            continue
        processed += 1  # Count only jobs actually run.
    return processed


# ----------------------------------------------------------------- repair
def repair_daily(dry_run: bool) -> int:
    """Rebuild dates with entries but missing/stale daily notes (entries persist if
    render locking failed or rendering stopped; reflect them in daily notes here)."""
    import summarize
    now = time.time()
    fixed = 0
    for memory_dir in PROJECTS_ROOT.glob("*"):
        if memory_dir.name.startswith("_") or is_excluded(project_id=memory_dir.name):
            continue
        for day_dir in (memory_dir / "entries").glob("????-??-??"):
            try:
                newest = max((p.stat().st_mtime for p in day_dir.glob("*.json")), default=0)
                if not newest or now - newest > MAX_AGE_DAYS * 86400:
                    continue
                daily = memory_dir / "daily" / f"{day_dir.name}.md"
                if daily.exists() and daily.stat().st_mtime >= newest:
                    continue
            except OSError:
                continue
            fixed += 1
            if not dry_run:
                summarize.rerender_daily(memory_dir, memory_dir / "state", day_dir.name)
    return fixed


def _refresh_vault() -> int:
    import vault
    return vault.refresh_all()


# ----------------------------------------------------------------------- main
def main(argv: list[str] | None = None) -> int:
    if os.environ.get(INTERNAL_ENV):
        return 0
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="sadece say, hicbir sey yazma/ozetleme")
    args = parser.parse_args(argv)

    lock = PROJECTS_ROOT / "_sweep.lock"
    from locks import acquire, release
    if not acquire(lock, LOCK_STALE_SECONDS):
        return 0  # Another sweep is running (its owner is alive).
    started = time.time()
    try:
        if not args.dry_run:
            try:
                import merge_projects
                merge_projects.resume_interrupted()
            except Exception:  # noqa: BLE001
                pass
        results = {}
        for name, step in (("claude", lambda: sweep_claude(args.dry_run)),
                           ("codex-kuyruga", lambda: enqueue_codex_missed(args.dry_run)),
                           ("codex-islenen", lambda: process_codex_jobs(args.dry_run)),
                           ("gunluk-onarim", lambda: repair_daily(args.dry_run)),
                           ("sayfalar", lambda: 0 if args.dry_run else _refresh_vault())):
            try:
                results[name] = step()
            except Exception as exc:  # noqa: BLE001 -- one step must not stop the others
                results[name] = f"hata:{exc.__class__.__name__}"
        if not args.dry_run:
            try:  # The startup health line reads this report.
                import health_report
                health_report.compute()
            except Exception as exc:  # noqa: BLE001
                results["saglik"] = f"hata:{exc.__class__.__name__}"
        claude = results["claude"]
        summary = (f"{'DRY ' if args.dry_run else ''}"
                   f"claude-proje={claude[0] if isinstance(claude, tuple) else claude} "
                   f"claude-islenen={claude[1] if isinstance(claude, tuple) else '-'} "
                   f"codex-kuyruga={results['codex-kuyruga']} codex-islenen={results['codex-islenen']} "
                   f"gunluk-onarim={results['gunluk-onarim']} sayfalar={results['sayfalar']} "
                   f"sure={time.time() - started:.0f}s")
        log_line(summary)
        print(summary)
    finally:
        release(lock)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
