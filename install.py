#!/usr/bin/env python3
"""Install (or uninstall) ai-memory.

    python install.py                              # Claude Code + Codex, English
    python install.py --language Turkish --ui-language tr --user-name Ada
    python install.py --exclude "scratch*" --exclude playground
    python install.py --no-codex                   # Claude Code only
    python install.py --uninstall                  # remove hooks + scheduled task, keep your data

What it does:
  1. Copies hooks/ and scripts/ into the target folder (default ~/.ai-memory).
     Your memory data in <target>/projects/ is never touched, so re-running it is how
     you upgrade.
  2. Writes <target>/config.json (only the options you pass change; the rest is kept).
  3. Adds three hooks to ~/.claude/settings.json and/or ~/.codex/hooks.json.
     Only the "hooks" key is edited; a timestamped backup is written first.
  4. Windows: registers a scheduled task that runs scripts/sweep_all.py every 30
     minutes (recovers sessions whose hooks never ran, retries failed summaries,
     writes the health report). Elsewhere it prints a cron line to add yourself.
  5. Codex: runs the summarizer isolation test (a canary file must stay unreadable).
     Codex summaries stay paused until it passes; re-run it after every Codex update.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent
CODE_DIRS = ("hooks", "scripts")
# Code folders of earlier versions: removed on upgrade so no stale copy is left behind.
LEGACY_DIRS = ("codex",)
TASK_NAME = "ai-memory sweep"
MARKER = ".ai-memory-install"  # marks a folder as an ai-memory installation


def ps_quote(value: str) -> str:
    """A PowerShell single-quoted literal (a quote inside is doubled)."""
    return "'" + value.replace("'", "''") + "'"


def check_target(target: Path, force: bool) -> None:
    """Refuse targets where replacing the code folders could destroy something."""
    home = Path.home().resolve()
    if target == REPO or REPO in target.parents or target in REPO.parents:
        raise SystemExit(f"refusing: target {target} overlaps the source repo {REPO}")
    if target == home or target.parent == target:
        raise SystemExit(f"refusing: target {target} is your home folder or a drive root")
    for agent_dir in (home / ".claude", home / ".codex"):
        if target == agent_dir or agent_dir in target.parents:
            raise SystemExit(f"refusing: {target} is inside {agent_dir}, the agent's own folder")
    if target.exists() and any(target.iterdir()) and not is_install(target) and not force:
        raise SystemExit(f"refusing: {target} is not empty and is not an ai-memory install "
                         "(use --force if you are sure)")


def is_install(target: Path) -> bool:
    """An existing install: our marker file, or the full layout of a version from
    before the marker existed. A bare projects/ folder is NOT enough (~/.claude has one)."""
    if (target / MARKER).exists():
        return True
    return ((target / "projects").is_dir() and (target / "scripts" / "summarize.py").is_file()
            and (target / "hooks" / "session-start.sh").is_file())

CODEX_HOOKS = {
    "SessionStart": ("codex_start.py", "", {"matcher": "startup|resume|clear|compact"}, 10),
    "SessionEnd": ("codex_capture.py", " --reason sessionend", {}, 3),
    "PreCompact": ("codex_capture.py", " --reason precompact", {"matcher": "manual|auto"}, 3),
}


def backup(path: Path) -> None:
    if path.exists():
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        shutil.copy2(path, path.with_name(f"{path.name}.backup-{stamp}"))


def atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    if path.exists():
        shutil.copymode(path, tmp)  # keep the user's permissions (e.g. 0600)
    os.replace(tmp, path)


def copy_code(target: Path) -> None:
    """Copy everything to a staging folder first, then swap it in; if the swap fails half
    way, the old code is put back. The installed code is never left half-deleted."""
    target.mkdir(parents=True, exist_ok=True)
    staging = target / f".install-new-{os.getpid()}"
    retired = target / f".install-old-{os.getpid()}"
    shutil.rmtree(staging, ignore_errors=True)
    for name in CODE_DIRS:
        shutil.copytree(REPO / name, staging / name, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    swapped: list[str] = []
    try:
        retired.mkdir()
        for name in CODE_DIRS:
            if (target / name).exists():
                os.replace(target / name, retired / name)
            os.replace(staging / name, target / name)
            swapped.append(name)
    except OSError:
        for name in CODE_DIRS:  # roll back: old code back in place
            if (retired / name).exists():
                if (target / name).exists():
                    shutil.rmtree(target / name, ignore_errors=True)
                os.replace(retired / name, target / name)
        raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)
        shutil.rmtree(retired, ignore_errors=True)
    for name in LEGACY_DIRS:
        shutil.rmtree(target / name, ignore_errors=True)
    (target / MARKER).write_text("ai-memory installation; code folders are replaced on upgrade\n",
                                 encoding="utf-8")
    for script in (target / "hooks").glob("*.sh"):
        script.chmod(0o755)
    projects = target / "projects"
    projects.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        projects.chmod(0o700)  # session summaries are private


def write_config(target: Path, args: argparse.Namespace) -> None:
    path = target / "config.json"
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        config = {}
    except (OSError, ValueError) as error:  # never overwrite a config we cannot read
        raise SystemExit(f"{path} could not be read ({error}); fix or move it, then re-run")
    if not isinstance(config, dict):
        raise SystemExit(f"{path} is not a JSON object; fix or move it, then re-run")
    for key, value in (("language", args.language), ("ui_language", args.ui_language),
                       ("user_name", args.user_name), ("codex_model", args.codex_model)):
        if value is not None:
            config[key] = value
    if args.exclude:
        config["exclude_projects"] = sorted(set(config.get("exclude_projects", [])) | set(args.exclude))
    atomic_write_json(path, config)


def claude_hooks(target: Path, settings: Path, action: str) -> None:
    backup(settings)
    subprocess.run(
        [sys.executable, str(target / "scripts" / "settings_merge.py"), str(settings), f"--{action}"],
        check=True,
    )


def codex_command(target: Path, script: str, args: str) -> str:
    return f'"{Path(sys.executable).as_posix()}" "{(target / "scripts" / script).as_posix()}"{args}'


def codex_command_windows(target: Path, script: str, args: str) -> str:
    """Codex runs hook commands through PowerShell on Windows, where a line that starts with
    a quoted path is a string, not a command ("SessionStart Failed"). Use the py launcher
    when present (proven), else PowerShell's call operator."""
    path = str(target / "scripts" / script)
    if shutil.which("py"):
        return f'py -3 "{path}"{args}'
    return f'& "{sys.executable}" "{path}"{args}'


def codex_hooks(target: Path, hooks_file: Path, action: str) -> None:
    try:
        data = json.loads(hooks_file.read_text(encoding="utf-8"))
    except FileNotFoundError:
        data = {}
    if not isinstance(data, dict):
        raise SystemExit(f"{hooks_file} is not a JSON object; not touching it")
    backup(hooks_file)
    hooks = data.setdefault("hooks", {})
    # Ours = commands pointing into this install (current scripts/ or the older codex/ folder).
    ours = ((target / "scripts").as_posix() + "/codex_", (target / "codex").as_posix() + "/")
    for event, (script, args, extra, timeout) in CODEX_HOOKS.items():
        groups = [g for g in hooks.get(event, []) if isinstance(g, dict)]
        # Drop any previous entry of ours (also makes re-install idempotent).
        for group in groups:
            group["hooks"] = [h for h in group.get("hooks", [])
                              if not any(mark in str(h.get("command", "")) for mark in ours)]
        groups = [g for g in groups if g.get("hooks")]
        if action == "add":
            hook = {"type": "command", "command": codex_command(target, script, args), "timeout": timeout}
            if os.name == "nt":
                hook["commandWindows"] = codex_command_windows(target, script, args)
            if event == "SessionStart":
                hook["additionalContextLimit"] = 16000  # room for both rules files + health line
            groups.append({**extra, "hooks": [hook]})
        if groups:
            hooks[event] = groups
        else:
            hooks.pop(event, None)
    if not hooks:
        data.pop("hooks", None)
    atomic_write_json(hooks_file, data)
    print(f"ok: {'added' if action == 'add' else 'removed'} hooks in {hooks_file}")
    if action == "add":
        print("   Codex asks you to review and trust changed hooks: open Codex and run /hooks.")


def _task_state() -> tuple[str, str]:
    """("none", "") if there is no task with our name, ("task", <its arguments>) if there
    is one (arguments may be empty), ("error", <message>) if the query itself failed.
    "Not found" is told apart from other errors by its category, not by the (localized)
    message, so an access error is never mistaken for "no task"."""
    script = (
        "try { $t = Get-ScheduledTask -TaskName " + ps_quote(TASK_NAME) + " -ErrorAction Stop; "
        "'TASK:' + (($t.Actions | ForEach-Object { $_.Arguments }) -join ' ') } "
        "catch { if ($_.CategoryInfo.Category -eq 'ObjectNotFound') { 'NONE:' } else { 'ERROR:' + $_ } }"
    )
    result = subprocess.run(["powershell", "-NoProfile", "-Command", script],
                            capture_output=True, text=True, check=False)
    out = result.stdout.strip()
    for prefix, state in (("TASK:", "task"), ("NONE:", "none")):
        if out.startswith(prefix):
            return state, out[len(prefix):].strip()
    return "error", (out[6:] if out.startswith("ERROR:") else out or result.stderr.strip())


def scheduler(target: Path, action: str, replace_task: bool = False) -> bool:
    sweep = target / "scripts" / "sweep_all.py"
    if os.name != "nt":
        if action == "add":
            print("Scheduled sweep: add this line with `crontab -e`:")
            print(f'   */30 * * * * "{sys.executable}" "{sweep}" >/dev/null 2>&1')
        return True
    launcher = target / "scripts" / "sweep_all.vbs"
    ours = f'"{launcher}"'
    state, existing = _task_state()
    if state == "error":
        # Never act on a task we could not inspect (it might belong to someone else).
        print(f"NOT CHANGED: could not query scheduled task '{TASK_NAME}': {existing}")
        return False
    if action == "remove":
        # Only if the task belongs to THIS installation: uninstalling a test or a second
        # copy must never remove the real task.
        if state == "task" and existing == ours:
            subprocess.run(["powershell", "-NoProfile", "-Command",
                            f"Unregister-ScheduledTask -TaskName {ps_quote(TASK_NAME)} -Confirm:$false"], check=False)
            print(f"ok: removed scheduled task '{TASK_NAME}'")
        elif state == "task":
            print(f"kept scheduled task '{TASK_NAME}': it belongs to another installation ({existing or 'no arguments'})")
        return True
    if state == "task" and existing != ours and not replace_task:
        print(f"NOT CHANGED: scheduled task '{TASK_NAME}' belongs to another installation "
              f"({existing or 'no arguments'}).\n   Uninstall that one first, or re-run with --replace-task.")
        return False
    # A tiny VBScript launcher runs Python in a hidden window; child processes
    # (claude, codex, git) share that hidden console, so nothing pops up.
    launcher.write_text(
        "' Scheduled task launcher: runs sweep_all.py in a hidden window.\n"
        'Set sh = CreateObject("WScript.Shell")\n'
        f'sh.Run """{sys.executable}"" ""{sweep}""", 0, True\n', encoding="utf-8")
    script = (
        f"$a = New-ScheduledTaskAction -Execute 'wscript.exe' -Argument {ps_quote(ours)}; "
        "$t = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(2) "
        "-RepetitionInterval (New-TimeSpan -Minutes 30); "
        "$s = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries "
        "-StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 1); "
        f"Register-ScheduledTask -TaskName {ps_quote(TASK_NAME)} -Description 'ai-memory: recover and summarize "
        "sessions every 30 min' -Action $a -Trigger $t -Settings $s -Force -ErrorAction Stop | Out-Null"
    )
    result = subprocess.run(["powershell", "-NoProfile", "-Command", script], check=False)
    ok = result.returncode == 0 and _task_state() == ("task", ours)
    print(f"{'ok' if ok else 'FAILED'}: scheduled task '{TASK_NAME}' (every 30 min)")
    return ok


def main() -> int:
    home = Path.home()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--target", type=Path, default=home / ".ai-memory")
    parser.add_argument("--language", help='summary language, e.g. "Turkish" (default: English)')
    parser.add_argument("--ui-language", choices=("en", "tr"), help="language of the system's own texts")
    parser.add_argument("--user-name", help="your name, used in the agent instructions")
    parser.add_argument("--codex-model", help="model for Codex summaries (default: Codex's own default)")
    parser.add_argument("--exclude", action="append", help="project folder name/pattern to leave out (repeatable)")
    parser.add_argument("--no-claude", action="store_true", help="skip Claude Code")
    parser.add_argument("--no-codex", action="store_true", help="skip Codex")
    parser.add_argument("--no-scheduler", action="store_true", help="do not register the 30-minute sweep")
    parser.add_argument("--skip-isolation-test", action="store_true",
                        help="do not run the Codex isolation test now (Codex summaries stay paused until it passes)")
    parser.add_argument("--claude-settings", type=Path, default=home / ".claude" / "settings.json")
    parser.add_argument("--codex-hooks", type=Path, default=home / ".codex" / "hooks.json")
    parser.add_argument("--replace-task", action="store_true",
                        help="take over the scheduled task even if it belongs to another installation")
    parser.add_argument("--force", action="store_true", help="install into a non-empty folder that is not an install")
    parser.add_argument("--uninstall", action="store_true")
    args = parser.parse_args()
    target = args.target.expanduser().resolve()
    check_target(target, args.force)

    if args.uninstall:
        if not args.no_claude and args.claude_settings.exists():
            claude_hooks(target, args.claude_settings, "remove")
        if not args.no_codex and args.codex_hooks.exists():
            codex_hooks(target, args.codex_hooks, "remove")
        if not args.no_scheduler:
            scheduler(target, "remove")
        print(f"Removed. Your memory data is still in {target / 'projects'}; delete it yourself if you want.")
        return 0

    if shutil.which("git") is None:
        print("warning: git not found; project ids will fall back to folder paths")
    write_config(target, args)  # first: refuses to continue if an existing config is unreadable
    copy_code(target)
    if not args.no_claude:
        if shutil.which("claude") is None:
            print("warning: `claude` CLI not found on PATH; sessions will not be summarized until it is")
        claude_hooks(target, args.claude_settings, "add")
    if not args.no_codex:
        codex_hooks(target, args.codex_hooks, "add")
        if shutil.which("codex") and not args.skip_isolation_test:
            print("Running the Codex summarizer isolation test (a few small model calls)...")
            subprocess.run([sys.executable, str(target / "scripts" / "codex_isolation_test.py")], check=False)
    scheduled = True if args.no_scheduler else scheduler(target, "add", args.replace_task)
    print(f"Installed into {target}. Start a new session to use it.")
    print('To save a rule, just tell the agent, e.g. "save this as a rule for this project: ..."')
    print('or "make this a global rule: ...". Suggested rules are listed in each project\'s candidates.md.')
    return 0 if scheduled else 1


if __name__ == "__main__":
    raise SystemExit(main())
