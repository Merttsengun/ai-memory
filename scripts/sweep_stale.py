#!/usr/bin/env python3
"""Recover hookin-*.json files whose summarize.py run never finished.

Incident (2026-08-26): a SessionEnd fired, session-end.sh wrote its
hook-input to state/hookin-<pid>.json and backgrounded summarize.py, but
that run left no entry AND no health.json -- meaning it never reached its
own try/finally at all (crashed before that point, or was killed). The
hook-input file was the only trace, and nothing ever came back to retry it.

This script is called from session-start.sh and session-end.sh, scoped to
the current project's own memory-dir, and sweeps any hookin-*.json older
than STALE_SECONDS (comfortably above summarize.py's own 180s claude-cli
timeout, so a run that is still legitimately in flight is never touched).
Claiming is a plain rename (no fcntl, Windows-native): whichever process
wins the rename processes the file, everyone else silently skips it.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
import summarize  # noqa: E402
from config import INTERNAL_ENV, load_config  # noqa: E402

STALE_SECONDS = 240
# If recovery dies, .recovered-PID files miss the glob and become orphaned;
# restore the original name once this old.
ORPHAN_CLAIM_SECONDS = 3600
# Scan missed sessions: hooks never ran (terminal closed), or the summarizer
# died. These accounted for most losses in the earlier review.
MISSED_MIN_IDLE_SECONDS = 30 * 60   # Leave sessions that may still be open alone.
MISSED_MAX_AGE_DAYS = 14
MISSED_PER_RUN = 2                  # New missed sessions to queue per run
JOBS_PER_RUN = 3                    # TOTAL summary calls per run (quota limit)


def release_orphan_claims(state_dir: Path, now: float) -> None:
    for claimed in state_dir.glob("hookin-*.json.recovered-*"):
        try:
            if now - claimed.stat().st_mtime < ORPHAN_CLAIM_SECONDS:
                continue
            original = claimed.with_name(claimed.name.split(".recovered-")[0])
            if not original.exists():
                claimed.rename(original)
        except OSError:
            continue


def _session_ids_in_jobs(state_dir: Path) -> set[str]:
    ids: set[str] = set()
    for path in list(state_dir.glob("hookin-*")) + list((state_dir / "failed").glob("*.json")):
        try:
            sid = json.loads(path.read_text(encoding="utf-8")).get("session_id")
        except (OSError, ValueError):
            continue
        if isinstance(sid, str):
            ids.add(sid)
    return ids


def _covered(state_dir: Path, sid: str, transcript: Path, mtime: float, done: dict[str, float]) -> bool:
    """Is everything in this transcript summarized already?

    The checkpoint is the real measure (it covers the turns of this generation). An old
    "sessionend" entry alone must NOT mark a session done forever: if it continued later
    and its end hook was missed, the new part has to be queued. Legacy sessions without a
    checkpoint fall back to done.txt / entries."""
    ckpt = summarize.checkpoint_path(state_dir, sid)
    if ckpt.exists():
        try:
            if ckpt.stat().st_mtime >= mtime:
                return True  # checkpoint written after the last change: cheap answer
            turns = summarize.read_transcript(transcript)
        except (OSError, ValueError):
            return False
        return summarize.read_checkpoint(ckpt, turns) >= len(turns)
    return mtime <= done.get(sid, -1.0) + 1


def enqueue_missed(memory_dir: Path, transcript_dir: Path, current_session: str, now: float) -> int:
    """Find finished, never-summarized sessions in this project's transcript folder
    and queue them as recovered jobs. Return the number of jobs added."""
    state_dir = memory_dir / "state"
    if not transcript_dir.is_dir():
        return 0
    # done: session -> transcript timestamp at summarization (0 = unknown time).
    # A transcript changed since then means the session continued; process it again.
    done: dict[str, float] = {}
    try:
        for line in (state_dir / "done.txt").read_text(encoding="utf-8").splitlines():
            sid, _, stamp = line.strip().partition("\t")
            if sid:
                done[sid] = max(done.get(sid, 0.0), float(stamp) if stamp else float("inf"))
    except (OSError, ValueError):
        pass
    for entry in (memory_dir / "entries").glob("*/*.json"):
        try:
            data = json.loads(entry.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and data.get("reason") == "sessionend":
            done[str(data.get("session_id"))] = float("inf")
    pending = _session_ids_in_jobs(state_dir)

    added = 0
    transcripts = sorted(transcript_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    for transcript in transcripts:
        if added >= MISSED_PER_RUN:
            break
        sid = transcript.stem
        if sid == current_session or sid in pending:
            continue
        try:
            mtime = transcript.stat().st_mtime
        except OSError:
            continue
        age = now - mtime
        if age < MISSED_MIN_IDLE_SECONDS or age > MISSED_MAX_AGE_DAYS * 86400:
            continue
        if _covered(state_dir, sid, transcript, mtime, done):
            continue
        job = state_dir / f"hookin-missed-{sid[:8]}.json"
        payload = {"session_id": sid, "transcript_path": str(transcript), "_reason": "recovered"}
        try:
            # "x": do not overwrite a job another sweeper just wrote.
            with job.open("x", encoding="utf-8") as fh:
                fh.write(json.dumps(payload))
        except FileExistsError:
            continue
        old = now - STALE_SECONDS - 1  # Process immediately.
        os.utime(job, (old, old))
        added += 1
    return added


def _not_before(path: Path) -> float:
    """For transient errors (limit etc.), summarize delays the job with _not_before."""
    try:
        value = json.loads(path.read_text(encoding="utf-8")).get("_not_before")
    except (OSError, ValueError, AttributeError):
        return 0.0
    return float(value) if isinstance(value, (int, float)) else 0.0


def main(argv: list[str] | None = None) -> int:
    if os.environ.get(INTERNAL_ENV):
        return 0

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--memory-dir", required=True, type=Path)
    parser.add_argument("--transcript-dir", type=Path)
    parser.add_argument("--current-session", default="")
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        return 0
    try:
        # Finish partial project merges here to avoid slowing startup.
        import merge_projects
        merge_projects.resume_interrupted()
    except Exception:
        pass
    run(args.memory_dir, args.transcript_dir, args.current_session, JOBS_PER_RUN)
    return 0


def run(memory_dir: Path, transcript_dir: Path | None, current_session: str, max_jobs: int) -> int:
    """Process a project's queue; return the job count (for the scheduler budget)."""
    state_dir = memory_dir / "state"
    if not state_dir.is_dir() or max_jobs <= 0:
        return 0

    now = time.time()
    try:
        release_orphan_claims(state_dir, now)
        if transcript_dir:
            enqueue_missed(memory_dir, transcript_dir, current_session, now)
        if load_config()["pause_summaries"]:
            return 0  # paused: jobs keep queueing, nothing is summarized
        candidates = sorted(state_dir.glob("hookin-*.json"))
    except OSError:
        return 0

    processed = 0
    for path in candidates:
        if processed >= max_jobs:
            break  # Leave the rest for the next run.
        try:
            age = now - path.stat().st_mtime
        except OSError:
            continue
        if age < STALE_SECONDS:
            continue  # might still be a legitimate in-flight run
        if _not_before(path) > now:
            continue  # Transient error (limit etc.): the waiting period has not elapsed.

        claimed = path.with_name(f"{path.name}.recovered-{os.getpid()}")
        try:
            path.rename(claimed)
        except OSError:
            continue  # another sweeper (or the original run) got it first
        processed += 1

        try:
            # summarize reads the reason from the job itself (_reason / hook_event_name);
            # this is only the fallback when both are missing.
            summarize.main(
                [
                    "--hook-input", str(claimed),
                    "--memory-dir", str(memory_dir),
                    "--reason", "sessionend",
                ]
            )
        except Exception as exc:  # sweep must never raise into the hook
            summarize.write_health(state_dir, f"sweep-recovery-failed:{exc.__class__.__name__}")

    return processed


if __name__ == "__main__":
    raise SystemExit(main())
