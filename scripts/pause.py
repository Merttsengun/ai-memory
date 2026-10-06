#!/usr/bin/env python3
"""Pause or resume ALL automatic summarizer calls (e.g. while measuring token use).

    python ~/.ai-memory/scripts/pause.py on      # pause: no summarizer model calls at all
    python ~/.ai-memory/scripts/pause.py off     # resume: waiting jobs are processed again
    python ~/.ai-memory/scripts/pause.py         # show the current state

While paused, session-end jobs still queue up (nothing is lost) and context is still
injected at session start; the health line shows that summaries are paused.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config import CONFIG_FILE, _read_json, load_config  # noqa: E402


def main(argv: list[str]) -> int:
    if not argv:
        print("paused" if load_config()["pause_summaries"] else "running")
        return 0
    if argv[0] not in ("on", "off"):
        print(__doc__)
        return 2
    data = _read_json(CONFIG_FILE)
    if data is None:
        print(f"{CONFIG_FILE} is not valid JSON; fix it first (nothing changed).")
        return 1
    data["pause_summaries"] = argv[0] == "on"
    tmp = CONFIG_FILE.with_name(f".{CONFIG_FILE.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, CONFIG_FILE)
    print("paused: no automatic summaries until `pause.py off`" if data["pause_summaries"]
          else "resumed: waiting jobs will be summarized again")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
