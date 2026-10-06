#!/usr/bin/env python3
"""Deterministic project id from a project path.

v1.3: the id no longer depends only on where the folder happens to live.

1. Git projects: the id comes from the repository's root commit, so moving
   the folder or opening a subfolder of the repo yields the same id.
   Repos whose top level is the home directory or a drive root are ignored
   (an umbrella repo there would swallow every project under it).
2. Everything else: the id comes from the normalized path, as before
   (same folder -> same id regardless of slash style, case, trailing sep).
   A renamed repo folder keeps its memory: an existing folder with the
   same root-commit digest is reused whatever its slug.
3. projects/aliases.json ({"old-id": "new-id"}) redirects an id that was
   explicitly merged into another one (merge_projects.py writes it). This
   is the only file ever looked up; index.json stays a human-readable log.

The legacy path-based id is still exposed (legacy_id) so callers can
detect and fold an older folder of the same project into the current one.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config import PROJECTS_ROOT  # noqa: E402  (AI_MEMORY_HOME override for tests)
ALIASES_FILE = PROJECTS_ROOT / "aliases.json"
GIT_TIMEOUT = 1.5


def normalize_path(path: str) -> str:
    # normcase folds case only where the file system is case-insensitive by convention
    # (Windows); on Linux /work/Foo and /work/foo are different folders and must keep
    # different ids. On Windows the result is unchanged from earlier versions.
    return os.path.normcase(os.path.normpath(os.path.abspath(path)))


def slugify(path: str) -> str:
    base = os.path.basename(os.path.normpath(path)) or "project"
    slug = re.sub(r"[^a-z0-9]+", "-", base.lower()).strip("-")
    return slug or "project"


def legacy_id(path: str) -> str:
    normalized = normalize_path(path)
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]
    return f"{slugify(path)}-{digest}"


def _git(path: str, *args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", path, *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=GIT_TIMEOUT,
            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _is_umbrella(toplevel: str) -> bool:
    normalized = normalize_path(toplevel)
    if os.path.dirname(normalized) == normalized:
        return True
    return normalized == normalize_path(os.path.expanduser("~"))


def git_identity(path: str) -> tuple[str, str] | None:
    toplevel = _git(path, "rev-parse", "--show-toplevel")
    if not toplevel or _is_umbrella(toplevel):
        return None
    roots = _git(path, "rev-list", "--max-parents=0", "HEAD")
    if not roots:
        return None
    return toplevel, sorted(roots.split())[0]


def load_aliases(aliases_file: Path = ALIASES_FILE) -> dict[str, str]:
    try:
        data = json.loads(aliases_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, str)}


def resolve_alias(pid: str, aliases: dict[str, str]) -> str:
    # Follows the whole chain; the seen-set alone stops a cycle.
    seen = {pid}
    while True:
        target = aliases.get(pid)
        if not target or target in seen:
            return pid
        seen.add(target)
        pid = target


def project_id(path: str, aliases_file: Path = ALIASES_FILE) -> str:
    git = git_identity(path)
    if git:
        toplevel, root_commit = git
        digest = hashlib.sha256(root_commit.encode("utf-8")).hexdigest()[:15]
        pid = f"{slugify(toplevel)}-g{digest}"
        if not (PROJECTS_ROOT / pid).is_dir():
            # Renamed repo folder: same root commit, different slug. Reuse
            # the existing memory instead of starting an empty one.
            existing = sorted(p.name for p in PROJECTS_ROOT.glob(f"*-g{digest}") if p.is_dir())
            if existing:
                pid = existing[0]
    else:
        pid = legacy_id(path)
    return resolve_alias(pid, load_aliases(aliases_file))


def main() -> int:
    args = sys.argv[1:]
    if len(args) == 2 and args[0] == "--legacy":
        print(legacy_id(args[1]))
        return 0
    if len(args) != 1:
        print("usage: project_id.py [--legacy] <path>", file=sys.stderr)
        return 1
    print(project_id(args[0]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
