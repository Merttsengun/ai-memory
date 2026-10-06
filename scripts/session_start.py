#!/usr/bin/env python3
"""SessionStart (Claude Code): build the memory context in a single Python process.

Why one process: the old bash hook made 4 Python + 4 git calls; on a cold start
it took 15-21 s, hit the 10 s timeout, and 6 of 49 sessions started with no
context at all. Now it is 1 Python process + 2 git calls; the heavy work
(unfinished summaries, missed sessions) runs in a detached background process.

build_context() is shared with the Codex SessionStart hook, so both agents see
exactly the same block.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
from config import INTERNAL_ENV, PROJECTS_ROOT, is_excluded, load_config, utf8_stdio  # noqa: E402
from texts import t  # noqa: E402

GLOBAL_RULES_BYTES = 12000
PROJECT_RULES_BYTES = 8000
DAILY_BYTES = 4000
BEHAVIOR_FILES = {"claude": "~/.claude/CLAUDE.md", "codex": "~/.codex/AGENTS.md"}


def read_capped(path: Path, limit: int) -> str:
    """Never cut silently: if over the cap, tell the agent and where to read the rest."""
    try:
        data = path.read_bytes()
    except OSError:
        return ""
    text = data[:limit].decode("utf-8", errors="ignore").strip()
    if len(data) > limit:
        text += t("truncated", size=len(data), limit=limit, path=path.as_posix())
    return text


def read_tail(path: Path, limit: int) -> str:
    try:
        data = path.read_bytes()
    except OSError:
        return ""
    return data[-limit:].decode("utf-8", errors="ignore").strip()


def latest_daily(project_data: Path) -> tuple[Path, int] | None:
    """Newest daily/YYYY-MM-DD.md and how many days old it is. (It used to be
    today/yesterday only, so a 2-day break meant no history at all.)"""
    today = dt.date.today()
    best = None
    try:
        for path in (project_data / "daily").glob("????-??-??.md"):
            try:
                day = dt.date.fromisoformat(path.stem)
            except ValueError:
                continue
            if day <= today and (best is None or day > best[1]):
                best = (path, day)
    except OSError:
        return None
    return (best[0], (today - best[1]).days) if best else None


def age_label(days: int) -> str:
    return t("age_today") if days == 0 else t("age_yesterday") if days == 1 else t("age_days", n=days)


def build_context(project_data: Path, agent: str = "claude") -> str:
    import health_report
    rules_path = project_data / "rules.md"
    where = [
        t("rules_title"),
        t("rules_project", path=rules_path.as_posix()),
        t("rules_global", path=(PROJECTS_ROOT / "rules.md").as_posix()),
        t("rules_behavior", path=BEHAVIOR_FILES.get(agent, BEHAVIOR_FILES["claude"])),
        t("rules_candidates", path=(project_data / "candidates.md").as_posix()),
        t("rules_policy"),
    ]
    extra = load_config()["extra_instructions"].strip()
    if extra:
        where.append(extra)
    parts = [health_report.start_block(project_data), "\n".join(where)]
    global_rules = read_capped(PROJECTS_ROOT / "rules.md", GLOBAL_RULES_BYTES)
    if global_rules:
        parts.append(t("global_header") + "\n" + global_rules)
    rules = read_capped(rules_path, PROJECT_RULES_BYTES)
    if rules:
        parts.append(t("project_header") + "\n" + rules)
    found = latest_daily(project_data)
    if found:
        daily, days = found
        text = read_tail(daily, DAILY_BYTES)
        if text:
            parts.append(t("daily_header", date=daily.stem, age=age_label(days)) + "\n" + text)
    return "\n\n".join(parts)


def _ensure_project_page(project_data: Path, folder: str | None = None) -> None:
    """A new project gets its page (and a line on the home page) right away, named after
    its folder (recorded now, not only at session end)."""
    try:
        import vault
        if folder and project_data.name not in vault.known_paths():
            from session_end import update_index
            update_index(project_data.name, folder)
        if not any(vault._is_generated(p) for p in project_data.glob("*.md")):
            vault.update(project_data)
    except Exception:  # noqa: BLE001 -- navigation must never break session start
        pass


def main() -> int:
    if os.environ.get(INTERNAL_ENV):
        return 0
    try:
        utf8_stdio(stdin=True, stdout=True)
        raw = sys.stdin.read()
        hook = json.loads(raw) if raw.strip() else {}
    except (OSError, ValueError):
        hook = {}
    project_dir = os.environ.get("CLAUDE_PROJECT_DIR") or hook.get("cwd")
    if not isinstance(project_dir, str) or not project_dir:
        return 0
    if is_excluded(project_dir):
        return 0  # left out of the memory system on purpose (config exclude_projects)

    import merge_projects
    import project_id as pid_mod
    from bg import spawn_detached

    project_id = pid_mod.project_id(project_dir)
    # Fold the old folder of a moved project / one opened from a subfolder (rarely does anything).
    try:
        lines = merge_projects.auto(project_dir, current=project_id, resume=False)
        if lines:
            (PROJECTS_ROOT / "_merged").mkdir(parents=True, exist_ok=True)
            with (PROJECTS_ROOT / "_merged" / "auto.log").open("a", encoding="utf-8") as log:
                log.write(f"{dt.datetime.now().isoformat(timespec='seconds')} {project_dir}\n")
                log.writelines(f"  {line}\n" for line in lines)
    except (OSError, ValueError):
        pass

    project_data = PROJECTS_ROOT / project_id
    state_dir = project_data / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    _ensure_project_page(project_data, project_dir)

    # Unfinished summaries + sessions whose hook never ran: detached, in the background.
    sweep = [sys.executable, str(SCRIPT_DIR / "sweep_stale.py"), "--memory-dir", str(project_data)]
    transcript = hook.get("transcript_path")
    if isinstance(transcript, str) and transcript:
        sweep += ["--transcript-dir", str(Path(transcript).parent),
                  "--current-session", str(hook.get("session_id") or "")]
    try:
        spawn_detached(sweep, state_dir / "lastrun-sweep.log")
    except OSError:
        pass

    context = build_context(project_data, "claude")
    # Move this project's orphaned Claude Code native memory after a folder move.
    # On any error or timeout it returns nothing; the output above is never broken.
    try:
        import native_migrate
        note = native_migrate.run_hook(project_dir, hook)
        if note:
            context = note + "\n\n" + context
    except Exception:  # noqa: BLE001
        pass
    import usage  # 7-day token tracking: how much context the memory adds to every session
    usage.record("inject-claude", project_id, usage.estimate_tokens(len(context)), chars=len(context))
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": context}},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
