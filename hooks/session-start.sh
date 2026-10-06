#!/bin/bash
[ -n "${AI_MEMORY_INTERNAL:-}" ] && exit 0
# SessionStart: all work runs in scripts/session_start.py, in one Python process.
# The old version made 4 Python + 4 git calls and exceeded the 10-second timeout
# on cold starts. Python may be a Windows Store stub; skip failed interpreters
# and try the next. Read stdin once and pass it unchanged to every attempt.
MEMORY_ROOT="$(CDPATH= cd "$(dirname "$0")/.." 2>/dev/null && pwd)"
HOOK_IN=$(cat)
for C in python python3 py; do
  command -v "$C" >/dev/null 2>&1 || continue
  OUT=$(printf '%s' "$HOOK_IN" | "$C" "$MEMORY_ROOT/scripts/session_start.py" 2>/dev/null) || continue
  [ -n "$OUT" ] && printf '%s\n' "$OUT"
  exit 0
done
exit 0
