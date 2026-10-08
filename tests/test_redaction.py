"""Redaction: common secret formats are masked, normal text stays, redact() is idempotent
(text is redacted twice: input and summary) and stays fast on hostile input.

Cases come from three rounds of independent review; each one once leaked or broke.
"""

from __future__ import annotations

import importlib
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

# (input, a part of the secret that must not survive)
LEAKS = [
    ("DB_PASSWORD=hunter2abc", "hunter2"), ("SECRET_KEY=abcd1234xyz", "abcd1234"),
    ("STRIPE_SECRET_KEY: sk_live_51Habcdefghijk", "51Hab"), ("sk_live_51Habcdefghijklmnop", "51Hab"),
    ("Authorization: Basic dXNlcjpwYXNz", "dXNlc"), ('"Authorization": "Token abcdef123456"', "abcdef"),
    ("Authorization: Basic dTpw", "dTpw"), ("Authorization: Bearer abcdefgh", "abcdefgh"),
    ("Authorization: Token abc", "abc"), ('Authorization: Digest username="bob", response="0123456789abcdef"', "0123456789"),
    ("mysql -uroot -pS3cretPw dbname", "S3cret"), ("mysql -p'abc123'", "abc123"), ('mysql -p"my secret password"', "secret"),
    ('mysql -p"abc"def', "def"), ("mysql -pfirstpass -psecondpass", "secondpass"), ("mysql " + " -ps" * 11 + " -pLAST", "LAST"),
    ("mysql -pmy\\ secret db", "secret"),
    ("sshpass -p S3cretPw ssh root@x", "S3cret"), ("sshpass -p 'my secret password' ssh host", "secret"),
    ('sshpass -p "hunter two" ssh host', "two"), ("sshpass -p foo -p bar ssh host", "bar"),
    ("sshpass -P prompt -psecret ssh host", "secret"), ("sshpass -vp hunter2 ssh user@host", "hunter2"),
    ("curl -u admin:S3cret https://x", "S3cret"), ("curl -uuser:password https://host", "password"),
    ("curl --user=user:password https://host", "password"), ("curl -u user:foo@bar https://host", "foo"),
    ('curl -u "user:two words" https://x', "words"), ("curl -u 'user:two words' https://x", "words"),
    ("curl -u user:firstpass -u user:secondpass https://host", "secondpass"), ("curl " + "x" * 301 + " -u user:secret", "secret"),
    ("curl -u \"user:abc'def\" https://example.com", "def"), ('curl -u "alice:abc\\"RealSecret123" https://x', "RealSecret"),
    ('curl -u alice:"RealSecret123" https://x', "RealSecret"), ("curl -u alice:my\\ secret https://x", "secret"),
    ('curl -u alice:foo\\"bar https://x', "bar"), ("curl --proxy-user user:hunter2 https://x", "hunter2"),
    ("curl -U user:hunter2 https://x", "hunter2"),
    ("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abcDEFghi123", "abcDEF"), ("eyJhbGciOiJIUzI1NiJ9.e30.c2ln", "c2ln"),
    ("eyJhbGciOiJIUzI1NiJ9.eyJhIjoxfQ.gsHw7Y20jSxZKv", "gsHw7"),
    ("password=ok1234", "ok1234"), ('"api_key": "abcd1234"', "abcd1234"), ("--password=S3cret", "S3cret"),
    ("PGPASSWORD=xyz mysql", "xyz"), ("GITHUB_TOKEN=abc123", "abc123"), ("JWT_SECRET=supersecret", "supersecret"),
    ('"client_secret":"abcq"', "abcq"), ("password: hunter2", "hunter2"), ("DB_PASSWORD=abc,def", "def"),
    ('"password": "foo\\"barsecret"', "barsecret"), ('{"password":"ab\\"cd ef","ok":1}', "cd ef"),
    ('"password":\n "secret"', "secret"), ("password =    secret", "secret"), ('password="abc"def', "def"),
    ('{"password": ["one", "two"]}', "two"), ('{"password": ["abc]RealSecret123"]}', "RealSecret"),
    ('{"credentials": [["firstSecret"], "secondSecret"]}', "Secret"), ("API_TOKEN=1234567890", "1234567890"),
    ("API_KEY=sk-abcdefghijklmnopqrstu!tail9", "tail9"), ("redis://:secret@host", "secret"),
    ("postgres://u:pw1@h/db", "pw1"), ("PWD=hunter2", "hunter2"),
]

UNCHANGED = [
    "max_tokens=4096", '"input_tokens": 4224', "tokens: 12", "token_count=5",
    "max_token=4096 input_token=120 total_token: 132", '{"total_token": "132", "ok": 1}', "total_token: 132, next: normal",
    "mkdir -p dir", "docker run -p 80:80 img", "ssh -p 22 root@x", "mysql -p dbname", "sshpass -e ssh -p 22 host",
    "mysql --help; docker run -p80:80 image", "curl https://example.com; docker run -u user:group image",
    "pk_live_abcdefghijklmnop", "https://example.com:8080/path", "PWD=/home/user/project", "PWD=C:\\Users\\user",
    "Authorization: Basic, mysql -p, sshpass -x", "curl -u alice https://x",
    "masking misses (DB_PASSWORD=, SECRET_KEY=, JWT, sk_live_ formats)",
]

HOSTILE = [
    'password="' + "." * 5000, "_" * 40000, "mysql " * 8000, "Authorization: " + " " * 40000, "a." * 20000,
    "curl " * 8000, "sshpass -e " * 8000, '"' + "a\\" * 20000, 'curl "' + "a" * 40000, "eyJabcde-" * 16000,
    "mysql -p" + '"a"' * 20000, "sshpass " + "-v" * 20000, ("password=[ " * 9091)[:100000],
    ("password=[[ " * 9091)[:100000], ('password="a ' * 9091)[:100000], ("password='a " * 9091)[:100000],
    ('curl -u "a ' * 9091)[:100000], ('mysql -p"a ' * 9091)[:100000], ('sshpass -p "a ' * 7000)[:100000],
    ('mysql "' * 16000)[:100000], ("curl -u a:\\" * 12000)[:100000], "Authorization: Digest " + "x" * 40000,
]


@pytest.fixture
def redaction(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / "memory"
    (home / "projects").mkdir(parents=True)
    monkeypatch.setenv("AI_MEMORY_HOME", str(home))
    if str(REPO / "scripts") not in sys.path:
        sys.path.insert(0, str(REPO / "scripts"))
    import config
    importlib.reload(config)
    import redaction as module
    return importlib.reload(module)


@pytest.mark.parametrize(("text", "secret"), LEAKS)
def test_secret_is_masked_once(redaction, text: str, secret: str) -> None:
    out = redaction.redact(text)
    assert redaction.MASK in out and secret not in out
    assert redaction.redact(out) == out  # the summary is redacted again


@pytest.mark.parametrize("text", UNCHANGED)
def test_normal_text_stays(redaction, text: str) -> None:
    assert redaction.redact(text) == text


def test_json_neighbours_survive(redaction) -> None:
    out = redaction.redact('{"password":"hunter2","ok":1}')
    assert out == '{"password":[REDACTED],"ok":1}' and redaction.redact(out) == out
    assert redaction.redact("mysql -psecret;docker -p80:80") == "mysql -p[REDACTED];docker -p80:80"


def test_hostile_input_is_linear(redaction) -> None:
    for text in HOSTILE:
        started = time.perf_counter()
        redaction.redact(text)
        assert time.perf_counter() - started < 1.0, text[:40]
