"""Token use of the memory system itself (for the 7-day tracking).

Every summarizer call and every context injection appends one JSON line to
projects/_usage.log:
  {"ts": ..., "kind": "summary-claude" | "summary-codex" | "inject-claude" | "inject-codex",
   "project": "<id>", "tokens": N, ...}
Summary tokens come from the CLIs themselves (claude -p usage; "tokens used" from
codex exec). Injection is measured in characters and estimated as tokens (~3.5 chars
per token), since the hook cannot see the agent's tokenizer. Never raises.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from config import PROJECTS_ROOT

USAGE_LOG = PROJECTS_ROOT / "_usage.log"
MAX_BYTES = 2 * 1024 * 1024
CHARS_PER_TOKEN = 3.5


def record(kind: str, project: str, tokens: int, **extra: object) -> None:
    try:
        USAGE_LOG.parent.mkdir(parents=True, exist_ok=True)
        if USAGE_LOG.exists() and USAGE_LOG.stat().st_size > MAX_BYTES:
            os.replace(USAGE_LOG, USAGE_LOG.with_suffix(".log.1"))
        line = {"ts": int(time.time()), "kind": kind, "project": project, "tokens": int(tokens), **extra}
        with USAGE_LOG.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(line, ensure_ascii=False) + "\n")
    except (OSError, ValueError, TypeError):
        pass


def estimate_tokens(chars: int) -> int:
    return round(chars / CHARS_PER_TOKEN)


def totals(since: float) -> dict[str, dict[str, int]]:
    """kind -> {"tokens": sum, "calls": count, "cost_usd_milli": sum} since a timestamp."""
    out: dict[str, dict[str, int]] = {}
    for path in (USAGE_LOG.with_suffix(".log.1"), USAGE_LOG):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for raw in lines:
            try:
                item = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(item, dict) or float(item.get("ts") or 0) < since:
                continue
            bucket = out.setdefault(str(item.get("kind")), {"tokens": 0, "calls": 0, "cost_usd_milli": 0})
            bucket["tokens"] += int(item.get("tokens") or 0)
            bucket["calls"] += 1
            bucket["cost_usd_milli"] += round(float(item.get("cost_usd") or 0) * 1000)
    return out
