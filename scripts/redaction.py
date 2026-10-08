"""Secret redaction: the SINGLE source for Claude and Codex summarizers.

Applied both to conversation text sent to the model and to the returned summary.
The Codex side (codex_common.py) imports it.
"""

from __future__ import annotations

import re

import sys
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config import load_config  # noqa: E402

# Read once per process (redact runs per message; the config does not change mid-run).
MASK = "[GİZLİ BİLGİ ÇIKARILDI]" if load_config()["ui_language"] == "tr" else "[REDACTED]"

# Text is redacted twice (input and summary): a value that already IS the mask is skipped,
# so redact(redact(x)) == redact(x).
_M = re.escape(MASK)
# Double-quoted string with escapes. The two alternatives never match the same character,
# so an unclosed quote cannot cause exponential backtracking; the length bound keeps many
# unclosed quotes on one long line from making the search quadratic.
_QUOTED = r'"(?:[^"\\\n]|\\.){0,2000}"' + r"|'[^'\n]{0,2000}'"
# One shell word: quoted parts, backslash escapes (my\ secret) and plain characters.
_WORD = r"(?:" + _QUOTED + r"|\\.|[^\s'\"\\])+"
# A JSON array value, one nesting level ([["a"], "b"]); bounded for the same reason.
_ARRAY = (r"\[(?:" + _QUOTED + r"|\[(?:" + _QUOTED + r"|[^\[\]\"'\n]){0,500}\]|[^\[\]\"'\n]){0,500}\]")
# The rest of one shell command: quoted strings, or anything but newline ; | & and quotes.
# "mysql --help; docker -p80:80" stops before docker. Linear: the alternatives do not overlap.
_CMD_REST = r"(?:" + _QUOTED + r"|\\.|[^\n;|&\"'\\])*"


def _keep_group1(m: re.Match[str]) -> str:
    return m.group(1) + MASK


def _user_password(m: re.Match[str]) -> str:
    """curl -u alice:"x" / "alice:x" -> -u alice:[mask]: the whole shell word after the first colon."""
    user, colon, _ = m.group(2).partition(":")
    if not colon:  # -u alice: curl asks for the password, nothing to mask
        return m.group(0)
    return m.group(1) + user.strip("\"'") + ":" + MASK


def _in_command(*options: re.Pattern[str] | tuple[re.Pattern[str], Callable[[re.Match[str]], str]]
                ) -> Callable[[re.Match[str]], str]:
    """Mask every password option inside one matched command (also repeated ones: -pA -pB)."""
    def repl(m: re.Match[str]) -> str:
        command = m.group(0)
        for option in options:
            pattern, mask = option if isinstance(option, tuple) else (option, _keep_group1)
            command = pattern.sub(mask, command)
        return command
    return repl


def _key_value(m: re.Match[str]) -> str:
    key, value = m.group(2).lower(), m.group(3).strip("\"',")
    # max_token=4096, "total_token": "132" are counters; a 10+ digit value may be a real token.
    if key == "token" and value.isdigit() and len(value) <= 9:
        return m.group(0)
    # PWD=/home/user, PWD=C:\Users is the working directory, not a password.
    if key == "pwd" and re.match(r"[/~]|[A-Za-z]:[\\/]", value):
        return m.group(0)
    return m.group(1) + MASK


_RULES: tuple[tuple[re.Pattern[str], str | Callable[[re.Match[str]], str]], ...] = (
    # Also match truncated or very long key blocks: without END, match to the text end.
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)"), MASK),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{16,}"), MASK),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"), MASK),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), MASK),
    (re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}|\bgithub_pat_[A-Za-z0-9_]{20,}"), MASK),
    (re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}"), MASK),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{30,}"), MASK),
    (re.compile(r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{10,}|\bwhsec_[A-Za-z0-9]{16,}"), MASK),  # Stripe
    # JWT: the header is always JSON (eyJ...); the payload may be short ({} -> e30).
    # Not starting inside a base64 run, so "eyJab-eyJab-..." is scanned once (was quadratic).
    (re.compile(r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{2,}\.[A-Za-z0-9_-]*"), MASK),
    # postgres://user:password@host, redis://:password@host -> mask only the password.
    # Scheme bounded: an unbounded one was quadratic on "a.a.a...".
    (re.compile(r"(?i)(\b[a-z][a-z0-9+.-]{0,30}://[^\s:/@]*:)(?!" + _M + r")[^\s@/]+(?=@)"), _keep_group1),
    # Authorization: Basic x / Bearer x / Token x / Digest ... -> keep the header name.
    (re.compile(r"(?i)(\bauthorization[\"']?\s{0,5}[:=]\s{0,5}[\"']?)(?!" + _M + r")"
                r"(?:(?:basic|bearer|token)[ \t]+[^\s\"',]+|digest[ \t]+[^\n]*"
                r"|(?!(?:basic|bearer|token|digest)\b)[A-Za-z0-9._~+/=:-]{4,})"), _keep_group1),
    # Command-line passwords, only inside that command: mysql -pX / -p'X', sshpass -p X, curl -u user:X
    (re.compile(r"(?i)\b(?:mysql|mysqldump|mysqladmin|mariadb)\b" + _CMD_REST),
     _in_command(re.compile(r"(\s-p)(?!" + _M + r")(?=[^\s,;.)\]])" + _WORD))),
    # Only sshpass's own options (before the command it runs): "sshpass -e ssh -p 22" is ssh's port.
    # Combined flags too: -vp X.
    (re.compile(r"\bsshpass(?:[ \t]+(?:-[a-zA-Z]*?[fdpP][ \t]*(?:" + _M + "|" + _WORD + r")|-[a-zA-Z]+))*"),
     _in_command(re.compile(r"((?<=\s)-[a-zA-Z]*?p[ \t]*)(?!" + _M + r")" + _WORD))),
    # curl -u / --user / -U / --proxy-user user:password -- the whole shell word, any quoting.
    (re.compile(r"\bcurl\b" + _CMD_REST),
     _in_command((re.compile(r"(\s(?:-[uU][ \t]*|--(?:proxy-)?user(?:[ \t]+|=)))(?![\"']?[^\s:\"']*:" + _M + r")"
                             r"(" + _WORD + r")"), _user_password))),
    # password=x, DB_PASSWORD=x, SECRET_KEY: x, --password=x, JSON "api_key": "x" -> keep the key name.
    # The key must END with the word: max_tokens=4096 and "tokens": 12 stay readable.
    # A value may contain commas (.env: X=a,b). A value that already is the mask is skipped; a mask
    # followed by a comma counts as finished too, so {"password":[mask],"ok":1} keeps "ok" on the next pass.
    (re.compile(r"(?i)((?<![A-Za-z0-9])[A-Za-z0-9_.-]{0,40}?(api[_-]?key|secret(?:[_-]?key)?|password|passwd"
                r"|pwd|token|access[_-]?key|private[_-]?key|client[_-]?secret|credentials?)(?![A-Za-z0-9])"
                r"[\"']?\s{0,20}[:=]\s{0,20})(?!" + _M + r"(?![^\s,;}\"']))"
                r"(" + _M + r"[^\s,;}]*|" + _ARRAY + r"|(?:" + _QUOTED + r")[^\s,;}\"']*|(?=[^\s,;}])[^\s;}]+)"),
     _key_value),
)


def redact(text: str) -> str:
    for pattern, repl in _RULES:
        text = pattern.sub(repl, text)
    return text
