#!/bin/bash
[ -n "${AI_MEMORY_INTERNAL:-}" ] && exit 0
# PreCompact: queue an intermediate summary before context compaction and start the summarizer
# independently of the terminal (scripts/session_end.py).
MEMORY_ROOT="$(CDPATH= cd "$(dirname "$0")/.." 2>/dev/null && pwd)"
HOOK_IN=$(cat)
for C in python python3 py; do
  command -v "$C" >/dev/null 2>&1 || continue
  printf '%s' "$HOOK_IN" | "$C" "$MEMORY_ROOT/scripts/session_end.py" --reason precompact >/dev/null 2>&1 && exit 0
done
exit 0
