#!/usr/bin/env python3
"""Render a daily Markdown note from immutable entry JSON files.

Pure function: reads entries/<date>/*.json and writes daily/<date>.md.
Never calls a model, so nothing here can be steered by injected text --
worst case an entry's own text ends up quoted verbatim in the note.

v1.2: which project's memory folder to use is passed explicitly via
--memory-dir (this script no longer lives inside that folder).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from texts import t  # noqa: E402
import vault  # noqa: E402


def render(entries_dir: Path, date_str: str) -> str:
    date_dir = entries_dir / date_str
    entries = []
    if date_dir.is_dir():
        for path in sorted(date_dir.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(data, dict):
                entries.append(data)

    lines = [
        "---",
        f"title: {date_str}",
        f"date: {date_str}",
        "type: daily-log",
        "---",
        f"# {date_str}",
        "",
    ]
    if not entries:
        lines.append(t("d_none"))
    for entry in entries:
        ts = entry.get("ts", "")
        reason = entry.get("reason", "")
        lines.append(t("d_session", ts=ts, reason=reason))
        summary = entry.get("summary") or ""
        if summary:
            lines.append("")
            lines.append(summary)
        for key, label in (("decisions", t("d_decisions")), ("next_steps", t("d_next")),
                           ("warnings", t("d_warnings"))):
            items = entry.get(key) or []
            if items:
                lines.append("")
                lines.append(f"**{label}:**")
                for item in items:
                    lines.append(f"- {item}")
        lines.append("")
    lines.append("---")
    lines.append(vault.daily_footer(entries_dir.parent.name))  # full-path links, readable labels
    lines.append("")
    return "\n".join(lines)


def render_candidates(memory_dir: Path) -> str:
    """Rule candidates: NEVER injected into sessions; only for Mert's review.
    Candidates moved to rules.md automatically disappear from the list."""
    try:
        rules = (memory_dir / "rules.md").read_text(encoding="utf-8").casefold()
    except OSError:
        rules = ""
    seen: set[str] = set()
    rows: list[tuple[str, str]] = []
    for path in sorted((memory_dir / "entries").glob("*/*.json"), reverse=True):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        for item in data.get("rule_candidates") or []:
            if not isinstance(item, str) or not item.strip():
                continue
            key = item.strip().casefold()
            if key in seen or key in rules:
                continue
            seen.add(key)
            rows.append((path.parent.name, item.strip()))

    pid = memory_dir.name
    lines = [t("c_title"), "", t("c_intro", rules=vault.rules_or_page_link(pid)), ""]
    if not rows:
        lines.append(t("c_none"))
    lines += [f"- {date}: {text}" for date, text in rows]
    lines.append("")
    return "\n".join(lines)


def write_atomic(target: Path, content: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, target)


def write_candidates(memory_dir: Path) -> None:
    write_atomic(memory_dir / "candidates.md", render_candidates(memory_dir))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--memory-dir", required=True, type=Path)
    parser.add_argument("--date", required=True)
    args = parser.parse_args()

    memory_dir: Path = args.memory_dir
    entries_dir = memory_dir / "entries"
    daily_dir = memory_dir / "daily"

    # Serialize read-build-write when two summaries finish at once in the same project.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from locks import held
    with held(memory_dir / "state" / "daily.lock", stale_seconds=120, wait_seconds=20) as got:
        if not got:
            return 3  # Lock unavailable: DO NOT WRITE; caller records health, scheduler repairs it.
        write_atomic(daily_dir / f"{args.date}.md", render(entries_dir, args.date))
        write_candidates(memory_dir)
    try:  # navigation pages (project page + home); never fails the daily note itself
        vault.update(memory_dir)
    except Exception:  # noqa: BLE001
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
