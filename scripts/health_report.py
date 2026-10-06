#!/usr/bin/env python3
"""Memory health.

Two parts:
  compute()     -- the scheduled sweep calls it every run; it counts the REAL
                   session files on disk and writes projects/_health.json.
                   The denominator is transcripts, not the job queue: even if a
                   hook never ran, the session shows up as "unaccounted".
  start_block() -- Claude/Codex session start; reads _health.json + the project's
                   own queue (a cheap glob) and renders a short block. No heavy work.

Every long session is in exactly one state: recorded / pending / failed / unaccounted.
The measurement rate is shown only when projects/_measure_start.txt (YYYY-MM-DD) exists.

By hand: python health_report.py          (compute + print)
"""

from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
from config import (  # noqa: E402
    CONFIG_FILE, PROJECTS_ROOT, SCHEDULED_TASK, SCRIPTS_DIR, config_problem, is_excluded, load_config,
    utf8_stdio,
)
from texts import t  # noqa: E402

HEALTH_FILE = PROJECTS_ROOT / "_health.json"
MEASURE_FILE = PROJECTS_ROOT / "_measure_start.txt"
SWEEP_LOG = PROJECTS_ROOT / "_sweep.log"
CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"
CODEX_SESSIONS = Path.home() / ".codex" / "sessions"

WINDOW_DAYS = 7
LONG_SESSION_CHARS = 15_000        # "long session": at least this much conversation text
MIN_IDLE_SECONDS = 30 * 60         # a session that may still be open is not counted
SCHEDULER_STALE_SECONDS = 2 * 3600
PENDING_OLD_SECONDS = 24 * 3600
# The sweep queues an idle session at the latest on its next run (30 min); before
# that, "not recorded, not queued" is not unaccounted, it is waiting its turn.
GRACE_SECONDS = MIN_IDLE_SECONDS + 60 * 60
STATES = ("recorded", "pending", "failed", "unaccounted")


# --------------------------------------------------------------------- helpers
def _measure_start() -> float | None:
    try:
        day = dt.date.fromisoformat(MEASURE_FILE.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    return dt.datetime.combine(day, dt.time()).timestamp()


def _ids_in(paths) -> set[str]:
    ids: set[str] = set()
    for path in paths:
        try:
            sid = json.loads(path.read_text(encoding="utf-8")).get("session_id")
        except (OSError, ValueError, AttributeError):
            continue
        if isinstance(sid, str):
            ids.add(sid)
    return ids


def _claude_done(memory_dir: Path) -> dict[str, float]:
    """Legacy fallback (sessions from before checkpoints were kept): session -> transcript
    time when summarized (a sessionend entry: infinite)."""
    done: dict[str, float] = {}
    try:
        for line in (memory_dir / "state" / "done.txt").read_text(encoding="utf-8").splitlines():
            sid, _, stamp = line.partition("\t")
            done[sid] = max(done.get(sid, 0.0), float(stamp) if stamp.strip().isdigit() else float("inf"))
    except OSError:
        pass
    for entry in (memory_dir / "entries").glob("*/*.json"):
        try:
            data = json.loads(entry.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if data.get("reason") == "sessionend":
            done[str(data.get("session_id"))] = float("inf")
    return done


def _last_sweep() -> float | None:
    try:
        lines = SWEEP_LOG.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        if " DRY " in f" {line} ":
            continue
        try:
            return dt.datetime.fromisoformat(line.split(" ", 1)[0]).timestamp()
        except ValueError:
            continue
    return None


def _codex_isolation() -> str:
    """'' = fine; else a short reason code: untested | changed:<version> | unknown."""
    import shutil
    codex = shutil.which("codex")
    if not codex:
        return ""  # Codex not installed: nothing to warn about
    try:
        from codex_common import ISOLATION_OK_FILE, isolation_stamp
        current = subprocess.run([codex, "--version"], capture_output=True, text=True,
                                 timeout=30, check=False).stdout.strip()
        tested = ISOLATION_OK_FILE.read_text(encoding="utf-8").strip() if ISOLATION_OK_FILE.exists() else ""
    except Exception:  # noqa: BLE001
        return "unknown"
    if not tested:
        return "untested"
    return "" if tested == isolation_stamp(current) else f"changed:{current}"


# --------------------------------------------------------------------- compute
def compute() -> dict:
    import project_id as pid_mod
    import summarize
    import sweep_all
    import codex_summarize
    from codex_common import transcript_messages

    now = time.time()
    since = max(now - WINDOW_DAYS * 86400, _measure_start() or 0)
    pid_cache: dict[str, str] = {}

    def pid_for(cwd: str) -> str | None:
        if cwd not in pid_cache:
            ok = os.path.isdir(cwd) and not is_excluded(cwd)
            pid_cache[cwd] = pid_mod.project_id(cwd) if ok else ""
        return pid_cache[cwd] or None

    totals = {"claude": dict.fromkeys(STATES, 0), "codex": dict.fromkeys(STATES, 0)}
    unaccounted: list[str] = []

    # Claude
    for tdir in CLAUDE_PROJECTS.glob("*"):
        files = [p for p in tdir.glob("*.jsonl") if since <= p.stat().st_mtime <= now - MIN_IDLE_SECONDS]
        if not files:
            continue
        cwd = sweep_all.usable_cwd(sweep_all.transcript_cwd(max(files, key=lambda p: p.stat().st_mtime)))
        pid = pid_for(cwd) if cwd else None
        if not pid:
            continue
        memory_dir = PROJECTS_ROOT / pid
        done = _claude_done(memory_dir)
        pending = _ids_in((memory_dir / "state").glob("hookin-*"))
        failed = _ids_in((memory_dir / "state" / "failed").glob("hookin-*"))
        for transcript in files:
            try:
                turns = summarize.read_transcript(transcript)
            except (OSError, ValueError):
                continue
            if sum(len(text) for _, text in turns) < LONG_SESSION_CHARS:
                continue
            sid = transcript.stem
            ckpt = summarize.checkpoint_path(memory_dir / "state", sid)
            if ckpt.exists():
                # The real measure: does this generation's processed turn count cover all
                # turns? (A session with only one part saved, or continued later, is NOT recorded.)
                recorded = summarize.read_checkpoint(ckpt, turns) >= len(turns)
            else:  # older entries from before checkpoints were kept
                recorded = done.get(sid, -1.0) + 1 >= transcript.stat().st_mtime
            state = ("recorded" if recorded else "pending" if sid in pending
                     else "failed" if sid in failed
                     else "pending" if now - transcript.stat().st_mtime < GRACE_SECONDS else "unaccounted")
            totals["claude"][state] += 1
            if state == "unaccounted":
                unaccounted.append(f"claude:{pid}:{sid[:8]}")

    # Codex
    for transcript in CODEX_SESSIONS.glob("*/*/*/rollout-*.jsonl"):
        mtime = transcript.stat().st_mtime
        if not (since <= mtime <= now - MIN_IDLE_SECONDS):
            continue
        meta = sweep_all._codex_meta(transcript)
        if not meta or meta.get("originator") == "codex_exec":
            continue
        cwd = sweep_all.usable_cwd(meta.get("cwd"))
        pid = pid_for(cwd) if cwd else None
        sid = meta.get("id")
        if not pid or not isinstance(sid, str):
            continue
        messages = transcript_messages(transcript) or []
        if len("\n\n".join(messages)) < LONG_SESSION_CHARS:
            continue
        state_dir = PROJECTS_ROOT / pid / "state"
        if messages and codex_summarize.already_done(state_dir, sid, messages) >= len(messages):
            state = "recorded"  # every message of this generation summarized
        elif sid in _ids_in(state_dir.glob("codex-hookin-*")):
            state = "pending"
        elif sid in _ids_in((state_dir / "failed").glob("codex-hookin-*")):
            state = "failed"
        elif now - mtime < GRACE_SECONDS:
            state = "pending"  # the sweep queues it on its next run
        else:
            state = "unaccounted"
        totals["codex"][state] += 1
        if state == "unaccounted":
            unaccounted.append(f"codex:{pid}:{sid[:8]}")

    # Sessions with a cut message (within the window)
    truncated = 0
    for log in PROJECTS_ROOT.glob("*/state/*health.log"):
        if is_excluded(project_id=log.parent.parent.name):
            continue
        try:
            for line in log.read_text(encoding="utf-8").splitlines():
                stamp = line.split(" ", 1)[0]
                if "truncated-" in line and dt.datetime.fromisoformat(stamp).timestamp() >= since:
                    truncated += 1
        except (OSError, ValueError):
            continue

    report = {
        "ts": int(now),
        "since": int(since),
        "measuring": _measure_start() is not None,
        "totals": totals,
        "unaccounted": unaccounted[:20],
        "truncated": truncated,
        "codex_isolation": _codex_isolation(),
        "usage": _usage_since(now - WINDOW_DAYS * 86400),
    }
    tmp = HEALTH_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, HEALTH_FILE)
    return report


def _usage_since(since: float) -> dict[str, int]:
    """Token use of the memory system itself over the window (7-day tracking)."""
    import usage
    kinds = usage.totals(since)
    summary = [v for k, v in kinds.items() if k.startswith("summary-")]
    inject = [v for k, v in kinds.items() if k.startswith("inject-")]
    return {
        "summary_tokens": sum(v["tokens"] for v in summary),
        "summary_calls": sum(v["calls"] for v in summary),
        "summary_cost_usd_milli": sum(v["cost_usd_milli"] for v in summary),
        "inject_tokens": sum(v["tokens"] for v in inject),
        "inject_sessions": sum(v["calls"] for v in inject),
        **{f"{k}_tokens": v["tokens"] for k, v in kinds.items()},
    }


# ------------------------------------------------------------------ start block
def _ago(seconds: float) -> str:
    if seconds < 3600:
        return t("h_ago_min", n=max(1, int(seconds // 60)))
    if seconds < 86400:
        return t("h_ago_hours", n=int(seconds // 3600))
    return t("h_ago_days", n=int(seconds // 86400))


def start_block(project_data: Path) -> str:
    """Short health block for session start. Never raises."""
    try:
        return _start_block(project_data)
    except Exception:  # noqa: BLE001
        return t("h_error")


def _start_block(project_data: Path) -> str:
    now = time.time()
    state = project_data / "state"
    warnings: list[str] = []

    # This project: last entry, pending, failed (cheap glob)
    entries = list((project_data / "entries").glob("*/*.json"))
    last = max((p.stat().st_mtime for p in entries), default=None)
    pending = list(state.glob("hookin-*.json")) + list(state.glob("codex-hookin-*.json"))
    failed = list((state / "failed").glob("*.json"))
    old_pending = [p for p in pending if now - p.stat().st_mtime > PENDING_OLD_SECONDS]
    project = t("h_project", last=_ago(now - last) if last else t("h_never"),
                pending=len(pending), failed=len(failed))
    if failed:
        warnings.append(t("w_failed", n=len(failed), path=(state / "failed").as_posix()))
    if old_pending:
        warnings.append(t("w_old_pending", n=len(old_pending), path=state.as_posix()))

    # Overall (the report the sweep writes)
    try:
        report = json.loads(HEALTH_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        report = None
    last_sweep = _last_sweep()
    if last_sweep is None or now - last_sweep > SCHEDULER_STALE_SECONDS:
        when = _ago(now - last_sweep) if last_sweep else t("w_sched_never")
        warnings.append(t("w_sched", when=when, task=SCHEDULED_TASK))
        general = t("h_sched_bad")
    else:
        general = t("h_sched_ok", time=dt.datetime.fromtimestamp(last_sweep).strftime("%H:%M"))

    # Checked live (cheap), not from the report: these change by hand at any time.
    if load_config()["pause_summaries"]:
        warnings.append(t("w_paused", cmd=(SCRIPTS_DIR / "pause.py").as_posix()))
    problem = config_problem()
    if problem:
        warnings.append(t("w_config", problem=problem, path=CONFIG_FILE.as_posix()))

    if report:
        used = report.get("usage") or {}
        if used.get("summary_calls") or used.get("inject_tokens"):
            general += " · " + t("h_usage", tokens=f"{used.get('summary_tokens', 0):,}",
                                 calls=used.get("summary_calls", 0), inject=f"{used.get('inject_tokens', 0):,}")
        reason = report.get("codex_isolation") or ""
        if reason:
            cmd = f"python {(SCRIPTS_DIR / 'codex_isolation_test.py').as_posix()}"
            warnings.append(t("w_codex_blocked", reason=reason, cmd=cmd))
        totals = report.get("totals", {})
        lost = sum(v.get("unaccounted", 0) for v in totals.values())
        if lost:
            warnings.append(t("w_lost", n=lost, list=", ".join(report.get("unaccounted", [])[:3])))
        if report.get("truncated"):
            warnings.append(t("w_truncated", n=report["truncated"]))
        if report.get("measuring"):
            start = dt.datetime.fromtimestamp(report["since"]).strftime("%d.%m")
            parts = [f"{label} {totals.get(name, {}).get('recorded', 0)}/{sum(totals.get(name, {}).values())}"
                     for name, label in (("claude", "Claude"), ("codex", "Codex"))]
            general += " · " + t("h_measure", date=start, parts=" · ".join(parts))

    lines = [f"{t('h_title')} {project} | {general}"]
    if warnings:
        lines += [f"⚠️ {w}" for w in warnings]
        lines.append(t("w_footer"))
    return "\n".join(lines)


if __name__ == "__main__":
    utf8_stdio(stdin=False, stdout=True)
    print(json.dumps(compute(), ensure_ascii=False, indent=2))
