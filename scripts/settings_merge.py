#!/usr/bin/env python3
"""Safely add/remove our three hook entries in a Claude Code settings.json.

Never a blind overwrite: the file is parsed as JSON, only our exact
command strings are added to (or removed from) the relevant hook event
arrays, and everything else in the file (model, theme, autoMode, ...) is
left byte-for-byte untouched. Written atomically (temp file + os.replace).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
from pathlib import Path
from typing import Any

MEMORY_ROOT = Path(__file__).resolve().parent.parent
HOOKS_DIR = MEMORY_ROOT / "hooks"

EVENTS = ("SessionStart", "SessionEnd", "PreCompact")


def _command_for(event: str, hooks_dir: Path | None = None) -> str:
    script = {
        "SessionStart": "session-start.sh",
        "SessionEnd": "session-end.sh",
        "PreCompact": "pre-compact.sh",
    }[event]
    path = ((hooks_dir or HOOKS_DIR) / script).as_posix()
    return f'"{path}"'


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    if path.exists():
        shutil.copymode(path, tmp)  # keep the user's permissions (e.g. 0600)
    os.replace(tmp, path)


def load_settings(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("settings.json is not a JSON object")
    return data


def add_hooks(settings: dict[str, Any]) -> dict[str, Any]:
    hooks = settings.setdefault("hooks", {})
    for event in EVENTS:
        command = _command_for(event)
        entries = hooks.setdefault(event, [])
        already_present = any(
            hook.get("command") == command
            for group in entries
            for hook in group.get("hooks", [])
        )
        if already_present:
            continue
        entries.append(
            {"hooks": [{"type": "command", "command": command, "timeout": 30 if event == "SessionStart" else 10}]}
        )
    return settings


def remove_hooks(settings: dict[str, Any], hooks_dir: Path | None = None) -> dict[str, Any]:
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return settings
    for event in EVENTS:
        command = _command_for(event, hooks_dir)
        entries = hooks.get(event)
        if not isinstance(entries, list):
            continue
        kept = []
        for group in entries:
            group_hooks = group.get("hooks", [])
            filtered = [h for h in group_hooks if h.get("command") != command]
            if filtered:
                kept.append({**group, "hooks": filtered})
        hooks[event] = kept
        if not hooks[event]:
            del hooks[event]
    if not hooks:
        settings.pop("hooks", None)
    return settings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("settings_path", type=Path)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--add", action="store_true")
    group.add_argument("--remove", action="store_true")
    parser.add_argument("--hooks-dir", type=Path,
                        help="with --remove: remove the hooks of ANOTHER installation (e.g. an old folder)")
    args = parser.parse_args()

    before = load_settings(args.settings_path)
    other_keys_before = {k: v for k, v in before.items() if k != "hooks"}

    settings = json.loads(json.dumps(before))  # deep copy
    if args.add:
        settings = add_hooks(settings)
    else:
        settings = remove_hooks(settings, args.hooks_dir)

    other_keys_after = {k: v for k, v in settings.items() if k != "hooks"}
    if other_keys_before != other_keys_after:
        raise RuntimeError("refusing to write: non-hooks keys would change")

    _atomic_write(args.settings_path, json.dumps(settings, indent=2, ensure_ascii=False) + "\n")
    print(f"ok: {'added' if args.add else 'removed'} hooks in {args.settings_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
