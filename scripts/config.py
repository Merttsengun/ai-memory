#!/usr/bin/env python3
"""Where the memory lives and how it behaves.

MEMORY_ROOT is the installed folder (the parent of scripts/), unless
AI_MEMORY_HOME points somewhere else (used by the tests). User settings live
in <MEMORY_ROOT>/config.json; anything missing or malformed falls back to
DEFAULTS, so a broken config never stops the hooks.
"""

from __future__ import annotations

import fnmatch
import io
import json
import os
import sys
from pathlib import Path
from typing import Any

MEMORY_ROOT = Path(os.environ.get("AI_MEMORY_HOME") or Path(__file__).resolve().parent.parent)
PROJECTS_ROOT = MEMORY_ROOT / "projects"
SCRIPTS_DIR = MEMORY_ROOT / "scripts"
CONFIG_FILE = MEMORY_ROOT / "config.json"
# Kept outside the code folders: re-installing replaces code, not this.
CODEX_ISOLATION_FILE = MEMORY_ROOT / "codex-isolation-ok.txt"

# Set on every summarizer child process; all hooks exit immediately when
# they see it, so a summarizer run can never trigger itself recursively.
INTERNAL_ENV = "AI_MEMORY_INTERNAL"
# Name of the Windows scheduled task the installer creates for sweep_all.py.
SCHEDULED_TASK = "ai-memory sweep"

DEFAULTS: dict[str, Any] = {
    # Language of the stored summaries (free text, passed to the model).
    "language": "English",
    # Language of what the agent and you read from the system itself
    # (context blocks, health line, daily notes): "en" or "tr".
    "ui_language": "en",
    # Name used in agent instructions ("only when <name> asks ..."); empty = "the user".
    "user_name": "",
    # Appended to the "where rules live" block, e.g. a pointer to your own skill.
    "extra_instructions": "",
    "claude_model": "haiku",
    # Empty = Codex's own default model. A small, cheap model is recommended.
    "codex_model": "",
    "codex_reasoning_effort": "medium",
    # Projects left completely out of the system (no injection, no summaries):
    # folder names or glob patterns, case-insensitive. E.g. ["*token", "scratch"].
    "exclude_projects": [],
    # Scheduled sweep: at most this many summarizer jobs per run.
    "claude_jobs_per_sweep": 6,
    "codex_jobs_per_sweep": 4,
    # Stop ALL automatic summarizer calls (e.g. while measuring token use). Jobs keep
    # queueing and are processed after you resume; context injection still works.
    "pause_summaries": False,
}

# Last config that parsed fine. If config.json gets corrupted, this one is used, so
# e.g. exclude_projects keeps working instead of silently falling back to "nothing excluded".
LAST_GOOD_FILE = MEMORY_ROOT / "config.lastgood.json"


def _valid(key: str, value: Any) -> bool:
    default = DEFAULTS[key]
    if isinstance(default, bool):
        return isinstance(value, bool)
    if isinstance(default, list):
        return isinstance(value, list) and all(isinstance(v, str) for v in value)
    if isinstance(default, int):
        return isinstance(value, int) and not isinstance(value, bool) and value >= 0
    return isinstance(value, str)


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):  # ValueError covers bad JSON and bad UTF-8
        return None
    return data if isinstance(data, dict) else None


def config_problem() -> str:
    """'' if config.json is fine (or absent); else a short description for the health line."""
    return "" if _read_json(CONFIG_FILE) is not None else f"{CONFIG_FILE.name} is not valid JSON"


def load_config() -> dict[str, Any]:
    data = _read_json(CONFIG_FILE)
    if data is None:  # corrupted: use the last good copy, never silently drop the settings
        data = _read_json(LAST_GOOD_FILE) or {}
    elif data:
        try:
            if _read_json(LAST_GOOD_FILE) != data:
                tmp = LAST_GOOD_FILE.with_name(f".{LAST_GOOD_FILE.name}.{os.getpid()}.tmp")
                tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
                os.replace(tmp, LAST_GOOD_FILE)
        except OSError:
            pass
    config = dict(DEFAULTS)
    config.update({k: v for k, v in data.items() if k in DEFAULTS and _valid(k, v)})
    if config["ui_language"] not in ("en", "tr"):
        config["ui_language"] = "en"
    if not config["language"].strip():
        config["language"] = DEFAULTS["language"]
    return config


def utf8_stdio(stdin: bool = True, stdout: bool = False) -> None:
    """Hook input/output is UTF-8 JSON; Windows consoles default to another code page.
    Only a real TextIOWrapper can be reconfigured: a replaced or missing stream (pythonw,
    test capture) is left alone instead of crashing the hook."""
    for name, wanted in (("stdin", stdin), ("stdout", stdout)):
        stream = getattr(sys, name, None)
        if wanted and isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(encoding="utf-8")


def is_excluded(project_dir: str | None = None, project_id: str | None = None) -> bool:
    """True if the project matches an exclude_projects pattern: the folder itself or
    ANY of its parent folders (so a subfolder of an excluded project is excluded too),
    or the readable slug at the start of its project id (covers jobs that only know the id)."""
    patterns = [p.lower() for p in load_config()["exclude_projects"] if p.strip()]
    if not patterns:
        return False
    names = []
    if project_dir:
        names += [part.lower() for part in Path(os.path.normpath(project_dir)).parts]
    if project_id:
        names.append(project_id.lower().rsplit("-", 1)[0])  # "<slug>-<hash>" -> slug
    return any(fnmatch.fnmatch(name, pattern) for name in names for pattern in patterns)
