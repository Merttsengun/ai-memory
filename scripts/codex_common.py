"""Shared parts of the Codex hooks.

- transcript_messages: reads the Codex rollout format
  ({"type": "response_item", "payload": {"type": "message", "role": ..., "content": [...]}}).
  An earlier reader looked for "item" / a top-level "role" and silently found nothing.
- is_claude_subagent: Codex launched from inside Claude Code (the CLAUDECODE env var
  is inherited). Those runs get no memory and are not summarized; their result is
  already in the main Claude session's conversation. A `codex exec` you run yourself
  is a normal session.
- isolation_flags: how the summarizer is boxed in (see codex_isolation_test.py).
- redact: from scripts/redaction.py, the single source shared with the Claude side.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config import CODEX_ISOLATION_FILE, load_config  # noqa: E402
from redaction import MASK, redact  # noqa: E402,F401

# Every feature that can read files or reach outside is switched off, and the
# user's config (MCP servers, plugins) is not loaded. The isolation test proves
# this with a canary file and records the Codex version + a digest of these
# flags; the summarizer only runs while both still match.
DISABLED_FEATURES = (
    "hooks", "shell_tool", "unified_exec", "apps", "browser_use", "browser_use_external",
    "computer_use", "plugins", "view_image", "code_mode_host", "skill_search", "tool_suggest",
)


def isolation_flags() -> list[str]:
    config = load_config()
    flags = ["--ephemeral", "--ignore-user-config", "--sandbox", "read-only", "--skip-git-repo-check"]
    for feature in DISABLED_FEATURES:
        flags += ["--disable", feature]
    if config["codex_model"].strip():
        flags += ["-m", config["codex_model"].strip()]
    if config["codex_reasoning_effort"].strip():
        flags += ["-c", f"model_reasoning_effort={config['codex_reasoning_effort'].strip()}"]
    return flags


def isolation_stamp(version: str, flags: list[str] | None = None) -> str:
    digest = hashlib.sha256("\0".join(flags or isolation_flags()).encode()).hexdigest()[:16]
    return f"{version}\n{digest}"


ISOLATION_OK_FILE = CODEX_ISOLATION_FILE

# Context blocks Codex adds itself (AGENTS.md, environment, permissions): not conversation.
INJECTED_BLOCK = re.compile(r"^\s*(?:<(?:[a-z_]+|permissions instructions)>|# AGENTS\.md instructions)", re.I)


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(_text(item) for item in value)
    if isinstance(value, dict) and isinstance(value.get("text"), str):
        return value["text"]
    return ""


def _message_text(content: Any) -> str:
    if not isinstance(content, list):
        return _text(content)
    parts = []
    for block in content:
        text = _text(block)
        if text and not INJECTED_BLOCK.match(text):
            parts.append(text)
    return "\n".join(parts)


def transcript_messages(path: Path) -> list[str] | None:
    """None: the file could not be read (retry). []: read, but no messages (empty session)."""
    messages: list[str] = []
    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return None
    for line in lines:
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict) or record.get("type") != "response_item":
            continue
        payload = record.get("payload")
        if not isinstance(payload, dict) or payload.get("type") != "message":
            continue
        role = payload.get("role")
        if role not in {"user", "assistant"}:
            continue
        # The whole message, redacted in full (no per-message cut at all: the summarizer
        # splits a very long message into pieces, so nothing is dropped).
        text = redact(_message_text(payload.get("content")).strip())
        if text:
            label = "User" if role == "user" else "Assistant"
            messages.append(f"**{label}:** {text}")
    return messages


def is_claude_subagent() -> bool:
    return bool(os.environ.get("CLAUDECODE"))
