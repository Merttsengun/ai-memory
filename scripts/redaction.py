"""Secret redaction: the SINGLE source for Claude and Codex summarizers.

Applied both to conversation text sent to the model and to the returned summary.
The Codex side (codex_common.py) imports it.
"""

from __future__ import annotations

import re

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config import load_config  # noqa: E402

# Read once per process (redact runs per message; the config does not change mid-run).
MASK = "[GİZLİ BİLGİ ÇIKARILDI]" if load_config()["ui_language"] == "tr" else "[REDACTED]"

# (pattern, preserve_groups) -- if True, keep groups 1 and 2 and mask what is between.
_PATTERNS: tuple[tuple[re.Pattern[str], bool], ...] = (
    # Also match truncated or very long key blocks: without END, match to the text end.
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)"), False),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{16,}"), False),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"), False),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), False),
    (re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}|\bgithub_pat_[A-Za-z0-9_]{20,}"), False),
    (re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}"), False),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{30,}"), False),
    # postgres://user:password@host -> mask only the password
    (re.compile(r"(?i)(\b[a-z][a-z0-9+.-]*://[^\s:/@]+:)[^\s@/]+(@)"), True),
    # password=x, password: x, and JSON form "password": "x"
    (re.compile(r"(?i)\b(?:api[_-]?key|secret|password|passwd|token|access[_-]?key)\b[\"']?\s*[:=]\s*"
                r"(?:\"[^\"\n]*\"|'[^'\n]*'|\S+)"), False),
)


def redact(text: str) -> str:
    for pattern, keep_groups in _PATTERNS:
        if keep_groups:
            text = pattern.sub(lambda m: m.group(1) + MASK + m.group(2), text)
        else:
            text = pattern.sub(MASK, text)
    return text
