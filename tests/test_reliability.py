"""Reliability tests: locks, generations, splitting into parts, continuation after an
interim save, duplicate protection, redaction, config/texts and the health line.

No real model is called: fake `claude` / `codex` executables echo back the MARK-n
markers they find in their input as the summary, so coverage can be asserted.
Everything runs against a throwaway AI_MEMORY_HOME; nothing touches the real setup.
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
FAKE_MODEL = r'''
import json, re, sys
args = sys.argv[1:]
if args and args[0] == "--version":
    print("codex-cli 0.0.0-fake"); sys.exit(0)
data = sys.stdin.read()
marks = sorted(set(re.findall(r"MARK-\d+", data)), key=lambda m: int(m.split("-")[1]))
summary = json.dumps({"summary": "covered " + " ".join(marks) if marks else "", "decisions": marks,
                      "next_steps": [], "warnings": [], "rule_candidates": []})
if "--output-format" in args and args[args.index("--output-format") + 1] == "json":  # claude -p
    print(json.dumps({"type": "result", "is_error": False, "result": summary, "total_cost_usd": 0.001,
                      "usage": {"input_tokens": len(data) // 4, "output_tokens": 50}}))
else:  # codex exec
    print(summary)
    print("tokens used\n1.234", file=sys.stderr)
'''


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Fresh AI_MEMORY_HOME + fake claude/codex on PATH; reloads the modules against it."""
    home = tmp_path / "memory"
    (home / "projects").mkdir(parents=True)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "fake_model.py").write_text(FAKE_MODEL, encoding="utf-8")
    for name in ("claude", "codex"):
        if os.name == "nt":
            (bindir / f"{name}.bat").write_text(f'@"{sys.executable}" "%~dp0fake_model.py" %*\n', encoding="utf-8")
        else:
            script = bindir / name
            script.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{bindir / "fake_model.py"}" "$@"\n')
            script.chmod(0o755)
    monkeypatch.setenv("AI_MEMORY_HOME", str(home))
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ["PATH"])
    monkeypatch.delenv("CLAUDECODE", raising=False)
    monkeypatch.delenv("AI_MEMORY_INTERNAL", raising=False)
    for path in (REPO / "scripts",):
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))
    for module in ("config", "texts", "redaction", "locks", "usage", "summarize", "codex_common", "codex_summarize",
                   "health_report", "session_start", "project_id", "merge_projects", "sweep_stale",
                   "vault", "render_daily", "sweep_all"):
        if module in sys.modules:
            importlib.reload(sys.modules[module])
    return home


def write_config(home: Path, **values) -> None:
    (home / "config.json").write_text(json.dumps(values), encoding="utf-8")
    import config
    import texts
    importlib.reload(config)
    importlib.reload(texts)


def entries(memory_dir: Path) -> list[dict]:
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(memory_dir.glob("entries/*/*.json"))]


# ------------------------------------------------------------------- locks
def test_lock_single_winner_and_owner_checks(env: Path, tmp_path: Path) -> None:
    import locks
    lock = tmp_path / "lk"
    lock.mkdir()
    (lock / "owner.json").write_text(json.dumps({"pid": 999999, "ts": time.time() - 999}))
    results: list[bool] = []
    threads = [threading.Thread(target=lambda: results.append(locks.acquire(lock, 60))) for _ in range(8)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert results.count(True) == 1  # stale lock taken over exactly once

    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        _, started = locks.process_identity(child.pid)
        (lock / "owner.json").write_text(json.dumps({"pid": child.pid, "start": started, "ts": time.time() - 99999}))
        assert locks.acquire(lock, 60) is False  # very old, but the owner is alive
        if started:  # start time is not readable everywhere (e.g. macOS has no /proc)
            (lock / "owner.json").write_text(json.dumps({"pid": child.pid, "start": started + 1,
                                                         "ts": time.time() - 999}))
            assert locks.acquire(lock, 60) is True  # same PID, different start time = PID reused
    finally:
        child.kill()
        child.wait()
    (lock / "owner.json").write_text(json.dumps({"pid": 12345678, "ts": time.time()}))
    locks.release(lock)
    assert lock.exists()  # someone else's lock is never removed
    assert locks.acquire(tmp_path / "deep" / "state" / "lk", 60)  # parent folders created


# -------------------------------------------------------------- generations
def test_checkpoint_generation_detects_rewrites(env: Path, tmp_path: Path) -> None:
    import summarize
    ckpt = tmp_path / "ck.json"
    turns = [("user", "a"), ("assistant", "b"), ("user", "c")]
    summarize.write_checkpoint(ckpt, 3, turns)
    assert summarize.read_checkpoint(ckpt, turns) == 3
    assert summarize.read_checkpoint(ckpt, turns + [("assistant", "d")]) == 3  # continued
    assert summarize.read_checkpoint(ckpt, turns[:2]) == 0  # shorter: new generation
    assert summarize.read_checkpoint(ckpt, [("user", "a"), ("assistant", "b"), ("user", "X")]) == 0


def test_split_covers_everything_without_overlap(env: Path) -> None:
    import summarize
    units = summarize.expand_turns([("user", "x" * 1000)] * 700, summarize.PIECE_CHARS)
    chunks = summarize.split_units(units, 300_000)
    assert chunks[0][0] == 0 and chunks[-1][1] == 700 and len(chunks) == 3
    assert all(a[1] == b[0] for a, b in zip(chunks, chunks[1:]))
    # A huge single message is split into pieces that together hold ALL of its text.
    huge = "A" * 400_000 + "MARK-7" + "B" * 400_000
    units = summarize.expand_turns([("user", "k"), ("user", huge), ("user", "k")], summarize.PIECE_CHARS)
    assert "".join(u[4] for u in units if u[0] == 1) == huge
    assert all(len(u[4]) <= summarize.PIECE_CHARS for u in units)


# ------------------------------------------------- Claude: parts + continuation
def _claude_transcript(path: Path, parts: list[str]) -> None:
    with path.open("a", encoding="utf-8") as fh:
        for i, text in enumerate(parts):
            role = "user" if i % 2 == 0 else "assistant"
            fh.write(json.dumps({"type": role, "message": {"role": role, "content": text}}) + "\n")


def _run_claude(memory: Path, transcript: Path, sid: str, reason: str) -> None:
    import summarize
    job = memory / "state" / f"hookin-{reason}.json"
    job.parent.mkdir(parents=True, exist_ok=True)
    job.write_text(json.dumps({"session_id": sid, "transcript_path": str(transcript),
                               "hook_event_name": "PreCompact" if reason == "precompact" else "SessionEnd"}))
    summarize.main(["--hook-input", str(job), "--memory-dir", str(memory), "--reason", reason])


def test_claude_parts_continuation_and_no_duplicates(env: Path, tmp_path: Path) -> None:
    import summarize
    summarize.MAX_TRANSCRIPT_CHARS = 3_000  # force several parts
    memory = env / "projects" / "app-aaaaaaaaaaaaaaaa"
    transcript = tmp_path / "s1.jsonl"
    filler = "filler text " * 40
    _claude_transcript(transcript, ["MARK-1 start decision"] + [filler] * 8 + ["MARK-2 middle decision"] + [filler] * 8)
    _run_claude(memory, transcript, "s1", "precompact")
    first = entries(memory)
    assert len(first) >= 2  # split into parts
    text1 = json.dumps(first)
    assert "MARK-1" in text1 and "MARK-2" in text1

    _claude_transcript(transcript, ["MARK-3 end decision", "ok"])
    _run_claude(memory, transcript, "s1", "sessionend")
    second = [e for e in entries(memory) if e not in first]
    assert second and "MARK-3" in json.dumps(second)
    assert "MARK-1" not in json.dumps(second)  # only the new part was summarized

    count = len(entries(memory))
    _run_claude(memory, transcript, "s1", "sessionend")  # same job again (e.g. resume / sweep)
    assert len(entries(memory)) == count  # no duplicate
    assert all("turn_range" in e and "anchor" in e for e in entries(memory))


# -------------------------------------------------- Codex: parts + continuation
def _codex_rollout(path: Path, project: Path, sid: str, texts: list[str], new: bool) -> None:
    with path.open("w" if new else "a", encoding="utf-8") as fh:
        if new:
            fh.write(json.dumps({"type": "session_meta", "payload": {"id": sid, "cwd": str(project),
                                                                     "originator": "codex-tui"}}) + "\n")
        for i, text in enumerate(texts):
            role = "user" if i % 2 == 0 else "assistant"
            kind = "input_text" if role == "user" else "output_text"
            fh.write(json.dumps({"type": "response_item", "payload": {
                "type": "message", "role": role, "content": [{"type": kind, "text": text}]}}) + "\n")


def _run_codex(memory: Path, project: Path, rollout: Path, sid: str, reason: str, monkeypatch) -> None:
    import codex_summarize
    state = memory / "state"
    state.mkdir(parents=True, exist_ok=True)
    job = state / f"codex-hookin-{reason}.json"
    job.write_text(json.dumps({"cwd": str(project), "session_id": sid, "transcript_path": str(rollout),
                               "reason": reason}))
    monkeypatch.setattr(sys, "argv", ["x", "--hook-input", str(job)])
    codex_summarize.main()


def test_codex_isolation_gate_parts_and_continuation(env: Path, tmp_path: Path, monkeypatch) -> None:
    import codex_common
    import codex_summarize
    from project_id import project_id
    codex_summarize.MAX_TRANSCRIPT_CHARS = 3_000
    project = tmp_path / "proj"
    project.mkdir()
    memory = env / "projects" / project_id(str(project))
    rollout = tmp_path / "rollout.jsonl"
    filler = "filler text " * 40
    _codex_rollout(rollout, project, "c1", ["MARK-1 start"] + [filler] * 8 + ["MARK-2 middle"] + [filler] * 8, True)

    _run_codex(memory, project, rollout, "c1", "precompact", monkeypatch)
    assert entries(memory) == []  # no isolation approval yet: blocked, job kept
    assert list((memory / "state").glob("codex-hookin-*.json"))

    codex_common.ISOLATION_OK_FILE.write_text(codex_common.isolation_stamp("codex-cli 0.0.0-fake"))
    _run_codex(memory, project, rollout, "c1", "precompact", monkeypatch)
    first = entries(memory)
    assert len(first) >= 2 and "MARK-1" in json.dumps(first) and "MARK-2" in json.dumps(first)

    _codex_rollout(rollout, project, "c1", ["MARK-3 end", "ok"], False)
    _run_codex(memory, project, rollout, "c1", "sessionend", monkeypatch)
    second = [e for e in entries(memory) if e not in first]
    assert "MARK-3" in json.dumps(second) and "MARK-1" not in json.dumps(second)

    count = len(entries(memory))
    _run_codex(memory, project, rollout, "c1", "sessionend", monkeypatch)
    assert len(entries(memory)) == count  # nothing new: no duplicate


# ------------------------------------------------------ redaction / validation
def test_redaction_formats_and_language(env: Path) -> None:
    import redaction
    text = ('Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.abcdefghijklmnop "password": "two words" '
            "postgres://admin:S3cret@db/x sk-proj-AbCdEf1234567890XyZ -----BEGIN RSA PRIVATE KEY-----\nMII" + "A" * 9000)
    out = redaction.redact(text)
    for secret in ("eyJhbGci", "two words", "S3cret", "sk-proj", "MIIA"):
        assert secret not in out
    assert "postgres://admin:[REDACTED]@db/x" in out
    write_config(env, ui_language="tr")
    importlib.reload(redaction)
    assert "[GİZLİ BİLGİ ÇIKARILDI]" in redaction.redact("token=abc")


def test_validation_is_strict(env: Path) -> None:
    import summarize
    good = {"summary": "s", "decisions": ["d"], "next_steps": [], "warnings": [], "rule_candidates": []}
    assert summarize.validate_summary(json.dumps(good))
    assert summarize.validate_summary(json.dumps({**good, "decisions": ["d", 3]})) is None  # non-string item
    assert summarize.validate_summary(json.dumps({**good, "extra": 1})) is None
    assert summarize.validate_summary(json.dumps({**good, "summary": "pw password=x"}))["summary"] == "pw [REDACTED]"


def test_publish_never_overwrites(env: Path, tmp_path: Path) -> None:
    import summarize
    assert summarize.publish_immutable(tmp_path / "a.json", "1")
    assert summarize.publish_immutable(tmp_path / "a.json", "2") is None
    assert (tmp_path / "a.json").read_text() == "1" and not list(tmp_path.glob(".*tmp"))


# ---------------------------------------------------------- config / texts
def test_exclude_projects_and_turkish_names(env: Path) -> None:
    write_config(env, exclude_projects=["token-lab", "*-scratch"], ui_language="tr", user_name="Mert")
    import config
    import texts
    assert config.is_excluded("C:/x/Token-Lab")
    assert config.is_excluded(project_id="token-lab-4b6f72905d27e3bc")
    assert config.is_excluded("/home/u/demo-scratch")
    assert not config.is_excluded("/home/u/my-client-site", "my-client-site-g1234567890abcde")
    assert "Mert'in" in texts.t("rules_policy") and "Mert'e" in texts.t("w_footer")
    assert texts._tr_suffix("Ali", "gen") == "Ali'nin" and texts._tr_suffix("Umut", "dat") == "Umut'a"


def test_start_block_and_context_in_both_languages(env: Path) -> None:
    import session_start
    project = env / "projects" / "app-aaaaaaaaaaaaaaaa"
    (project / "state" / "failed").mkdir(parents=True)
    (project / "state" / "failed" / "hookin-x.json").write_text("{}")
    context = session_start.build_context(project, "codex")
    assert "[Memory health]" in context and "failed 1" in context and "~/.codex/AGENTS.md" in context
    assert "⚠️" in context  # a failed job is a warning
    write_config(env, ui_language="tr", user_name="Mert")
    importlib.reload(sys.modules["health_report"])
    importlib.reload(session_start)
    context = session_start.build_context(project, "claude")
    assert "[Hafıza sağlığı]" in context and "başarısız 1" in context and "Mert'e" in context


# ------------------------------------------------- long single message: nothing lost
def test_marker_in_the_middle_of_a_huge_message_is_summarized(env: Path, tmp_path: Path) -> None:
    import summarize
    summarize.MAX_TRANSCRIPT_CHARS = 6_000
    summarize.PIECE_CHARS = 5_000
    memory = env / "projects" / "app-aaaaaaaaaaaaaaaa"
    transcript = tmp_path / "huge.jsonl"
    huge = "MARK-1 " + "x" * 20_000 + " MARK-2 " + "y" * 20_000 + " MARK-3"
    _claude_transcript(transcript, [huge, "ok"])
    _run_claude(memory, transcript, "h1", "sessionend")
    text = json.dumps(entries(memory))
    assert all(m in text for m in ("MARK-1", "MARK-2", "MARK-3"))
    assert any(e.get("pieces") for e in entries(memory))
    count = len(entries(memory))
    _run_claude(memory, transcript, "h1", "sessionend")
    assert len(entries(memory)) == count  # pieces recognized, no duplicates


def test_codex_huge_message_is_not_cut(env: Path, tmp_path: Path, monkeypatch) -> None:
    import codex_common
    import codex_summarize
    from project_id import project_id
    codex_summarize.MAX_TRANSCRIPT_CHARS = 6_000
    codex_summarize.PIECE_CHARS = 5_000
    codex_common.ISOLATION_OK_FILE.write_text(codex_common.isolation_stamp("codex-cli 0.0.0-fake"))
    project = tmp_path / "p2"
    project.mkdir()
    memory = env / "projects" / project_id(str(project))
    rollout = tmp_path / "r2.jsonl"
    _codex_rollout(rollout, project, "c2", ["MARK-1 " + "x" * 20_000 + " MARK-2 " + "y" * 20_000 + " MARK-3", "ok"], True)
    _run_codex(memory, project, rollout, "c2", "sessionend", monkeypatch)
    assert all(m in json.dumps(entries(memory)) for m in ("MARK-1", "MARK-2", "MARK-3"))


# ----------------------------------------------------------- strictness
def test_all_five_fields_required_and_warning_only_kept(env: Path) -> None:
    import summarize
    assert summarize.validate_summary(json.dumps({"summary": "only this"})) is None
    warn = summarize.validate_summary(json.dumps({"summary": "", "decisions": [], "next_steps": [],
                                                  "warnings": ["disk almost full"], "rule_candidates": []}))
    assert warn and summarize.has_content(warn)


def test_prefix_generation_catches_an_edit_in_the_middle(env: Path) -> None:
    import codex_summarize
    import summarize
    turns = [("user", "a"), ("assistant", "b"), ("user", "c")]
    edited = [("user", "a"), ("assistant", "CHANGED"), ("user", "c")]
    assert summarize.turn_anchor(turns, 3) != summarize.turn_anchor(edited, 3)
    assert codex_summarize.msg_anchor(["a", "b", "c"], 3) != codex_summarize.msg_anchor(["a", "X", "c"], 3)


# ----------------------------------------------------------- pause + exclusion
def test_pause_keeps_jobs_and_makes_no_calls(env: Path, tmp_path: Path) -> None:
    import summarize
    import usage
    write_config(env, pause_summaries=True)
    importlib.reload(summarize)
    memory = env / "projects" / "app-aaaaaaaaaaaaaaaa"
    transcript = tmp_path / "p.jsonl"
    _claude_transcript(transcript, ["MARK-1 decision", "ok"])
    _run_claude(memory, transcript, "p1", "sessionend")
    assert entries(memory) == [] and list((memory / "state").glob("hookin-*.json"))  # job kept
    assert usage.totals(0) == {}  # no model call made
    write_config(env, pause_summaries=False)
    importlib.reload(summarize)
    _run_claude(memory, transcript, "p1", "sessionend")
    assert "MARK-1" in json.dumps(entries(memory))
    assert usage.totals(0)["summary-claude"]["calls"] == 1  # token use recorded


def test_exclusion_covers_subfolders_and_queued_jobs(env: Path, tmp_path: Path) -> None:
    write_config(env, exclude_projects=["secret"])
    import config
    import summarize
    assert config.is_excluded(str(tmp_path / "secret" / "src"))
    memory = env / "projects" / "secret-aaaaaaaaaaaaaaaa"
    transcript = tmp_path / "x.jsonl"
    _claude_transcript(transcript, ["MARK-1", "ok"])
    _run_claude(memory, transcript, "x1", "sessionend")  # job queued before the project was excluded
    assert entries(memory) == []


# ----------------------------------------------------------- merge progress
def test_merge_moves_progress_files(env: Path) -> None:
    import merge_projects
    importlib.reload(merge_projects)
    src = env / "projects" / "old-aaaaaaaaaaaaaaaa" / "state"
    dst = env / "projects" / "new-bbbbbbbbbbbbbbbb" / "state"
    src.mkdir(parents=True)
    dst.mkdir(parents=True)
    (src / "checkpoint-s1.json").write_text(json.dumps({"processed_turns": 9}))
    (src / "done.txt").write_text("s1\t100\n")
    (dst / "done.txt").write_text("s2\t200\n")
    merge_projects.merge("old-aaaaaaaaaaaaaaaa", "new-bbbbbbbbbbbbbbbb")
    assert json.loads((dst / "checkpoint-s1.json").read_text())["processed_turns"] == 9
    assert (dst / "done.txt").read_text().splitlines() == ["s1\t100", "s2\t200"]


# ----------------------------------------------------------- installer safety
def _install(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(REPO / "install.py"), "--no-scheduler", "--skip-isolation-test",
                           "--no-claude", "--no-codex", *args], capture_output=True, text=True)


def test_installer_refuses_dangerous_targets(tmp_path: Path) -> None:
    assert _install("--target", str(REPO)).returncode != 0
    assert (REPO / "scripts" / "summarize.py").exists()  # source untouched
    stranger = tmp_path / "stranger"
    stranger.mkdir()
    (stranger / "hooks").mkdir()
    (stranger / "keep.txt").write_text("mine")
    assert _install("--target", str(stranger)).returncode != 0
    assert (stranger / "hooks").is_dir() and (stranger / "keep.txt").exists()
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / "projects").mkdir()
    (broken / "config.json").write_text("{not json")
    assert _install("--target", str(broken)).returncode != 0
    assert (broken / "config.json").read_text() == "{not json"  # never overwritten


def test_installer_upgrade_keeps_data(tmp_path: Path) -> None:
    target = tmp_path / "inst"
    assert _install("--target", str(target), "--exclude", "x").returncode == 0
    (target / "projects" / "p").mkdir()
    assert _install("--target", str(target)).returncode == 0
    assert (target / "projects" / "p").is_dir() and (target / "scripts" / "summarize.py").exists()
    assert not list(target.glob(".install-*"))  # no staging leftovers
    assert json.loads((target / "config.json").read_text())["exclude_projects"] == ["x"]


def test_upgrade_from_separate_codex_folder(tmp_path: Path) -> None:
    """Older installs kept the Codex hooks in <target>/codex/. An upgrade must remove that
    folder and re-point the Codex hooks to scripts/, without duplicating them."""
    target = tmp_path / "inst"
    hooks_file = tmp_path / "codex" / "hooks.json"
    (target / "codex").mkdir(parents=True)
    (target / "codex" / "codex_start.py").write_text("# old")
    (target / "projects").mkdir()
    (target / ".ai-memory-install").write_text("older install")  # older versions wrote this marker too
    old_cmd = f'"python" "{(target / "codex" / "codex_start.py").as_posix()}"'
    hooks_file.parent.mkdir()
    hooks_file.write_text(json.dumps({"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": old_cmd}]}]}}))
    result = subprocess.run([sys.executable, str(REPO / "install.py"), "--target", str(target), "--no-scheduler",
                             "--skip-isolation-test", "--no-claude", "--codex-hooks", str(hooks_file)],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert not (target / "codex").exists() and (target / "scripts" / "codex_start.py").exists()
    commands = [h["command"] for g in json.loads(hooks_file.read_text())["hooks"]["SessionStart"] for h in g["hooks"]]
    assert len(commands) == 1 and "/scripts/codex_start.py" in commands[0]


def test_installer_never_treats_an_agent_folder_as_an_install(tmp_path: Path) -> None:
    """~/.claude has its own projects/ (and often scripts/): it must never be taken for an
    install whose code folders may be replaced."""
    fake_home = tmp_path / "home"
    claude_dir = fake_home / ".claude"
    (claude_dir / "projects").mkdir(parents=True)
    (claude_dir / "scripts").mkdir()
    (claude_dir / "scripts" / "statusline.js").write_text("mine")
    env = {**os.environ, "HOME": str(fake_home), "USERPROFILE": str(fake_home)}
    for target in (claude_dir, claude_dir / "ai-memory"):
        result = subprocess.run([sys.executable, str(REPO / "install.py"), "--target", str(target), "--no-scheduler",
                                 "--skip-isolation-test", "--no-claude", "--no-codex"],
                                capture_output=True, text=True, env=env)
        assert result.returncode != 0
    assert (claude_dir / "scripts" / "statusline.js").read_text() == "mine"
    lookalike = tmp_path / "lookalike"
    (lookalike / "projects").mkdir(parents=True)
    (lookalike / "scripts").mkdir()
    sys.path.insert(0, str(REPO))
    import install
    assert not install.is_install(lookalike)  # a bare projects/ is not enough


def test_path_ids_keep_case_where_the_file_system_does(env: Path) -> None:
    import posixpath
    import project_id
    if os.name == "nt":
        assert project_id.normalize_path("C:/Work/Foo") == project_id.normalize_path("c:/work/foo")
    else:
        assert project_id.normalize_path("/work/Foo") != project_id.normalize_path("/work/foo")
    assert posixpath.normcase("/work/Foo") != posixpath.normcase("/work/foo")


# ------------------------------------------------------------------ vault pages
def _resolve_links(vault_root: Path) -> list[str]:
    """Every [[target|label]] in the vault must point to an existing note: a path inside
    the vault (with or without .md) or a unique note name, like Obsidian resolves them."""
    import re
    names: dict[str, list[Path]] = {}
    for md in vault_root.rglob("*.md"):
        names.setdefault(md.stem.casefold(), []).append(md)
    broken = []
    for md in vault_root.rglob("*.md"):
        for target in re.findall(r"\[\[([^\]|#\\]+)", md.read_text(encoding="utf-8")):
            path = vault_root / (target if target.endswith(".md") else target + ".md")
            if path.exists():
                continue
            if "/" not in target and len(names.get(target.casefold(), [])) == 1:
                continue
            broken.append(f"{md.relative_to(vault_root)} -> {target}")
    return broken


def _project(home: Path, pid: str, path: str, day: str = "", summary: str = "") -> Path:
    project = home / "projects" / pid
    (project / "state").mkdir(parents=True, exist_ok=True)
    index = home / "projects" / "index.json"
    data = json.loads(index.read_text(encoding="utf-8")) if index.exists() else {}
    data[pid] = path
    index.write_text(json.dumps(data), encoding="utf-8")
    if day:
        (project / "entries" / day).mkdir(parents=True, exist_ok=True)
        (project / "entries" / day / f"120000-{pid[:6]}.json").write_text(json.dumps({
            "session_id": "s", "reason": "sessionend", "ts": f"{day}T12:00:00", "summary": summary,
            "decisions": [], "next_steps": [], "warnings": [], "rule_candidates": []}), encoding="utf-8")
        subprocess.run([sys.executable, str(REPO / "scripts" / "render_daily.py"), "--memory-dir", str(project),
                        "--date", day], check=True, env={**os.environ, "AI_MEMORY_HOME": str(home)})
    return project


def test_same_named_projects_get_distinct_pages_and_links_resolve(env: Path) -> None:
    import vault
    a = _project(env, "app-aaaaaaaaaaaaaaaa", "/work/clients/app", "2026-10-01", "client A work")
    b = _project(env, "app-bbbbbbbbbbbbbbbb", "/work/personal/app", "2026-10-02", "personal work")
    (env / "projects" / "rules.md").write_text("global", encoding="utf-8")
    (a / "rules.md").write_text("client A rules", encoding="utf-8")
    vault.refresh_all()
    names = vault.display_names([a.name, b.name])
    assert names[a.name] == "app (clients)" and names[b.name] == "app (personal)"
    assert (a / "app (clients).md").exists() and (b / "app (personal).md").exists()
    home = (env / "projects" / "Home.md").read_text(encoding="utf-8")
    assert "app (clients)" in home and "app (personal)" in home
    assert "[[app-aaaaaaaaaaaaaaaa/rules|app (clients) rules]]" in (a / "app (clients).md").read_text(encoding="utf-8")
    assert _resolve_links(env / "projects") == []


def test_new_entry_updates_project_page_and_home(env: Path) -> None:
    project = _project(env, "site-cccccccccccccccc", "/work/site", "2026-10-03", "first day")
    page = project / "site.md"
    assert page.exists() and "first day" in page.read_text(encoding="utf-8")  # render_daily -> vault.update
    _project(env, "site-cccccccccccccccc", "/work/site", "2026-10-04", "second day")
    text = page.read_text(encoding="utf-8")
    assert text.index("site-cccccccccccccccc/daily/2026-10-04") < text.index("site-cccccccccccccccc/daily/2026-10-03")
    assert "site" in (env / "projects" / "Home.md").read_text(encoding="utf-8")


def test_new_project_gets_a_page_at_session_start(env: Path, tmp_path: Path) -> None:
    import session_start
    folder = tmp_path / "brand-new"
    folder.mkdir()
    data = env / "projects" / "brand-new-dddddddddddddddd"
    (data / "state").mkdir(parents=True)
    import vault
    vault.new_project(data, str(tmp_path / "Brand_New"))  # what session start runs in the background
    assert (data / "Brand_New.md").exists()
    assert "[[brand-new-dddddddddddddddd/Brand_New|Brand_New]]" in (env / "projects" / "Home.md").read_text(encoding="utf-8")


def test_legacy_footers_are_fixed_without_touching_content(env: Path) -> None:
    import vault
    project = _project(env, "old-eeeeeeeeeeeeeeee", "/work/old")
    (project / "daily").mkdir()
    body = "# 2026-09-01\n\n## Session 2026-09-01T10:00 (sessionend)\n\nkept text [[rules]] inside\n\n---\n"
    (project / "daily" / "2026-09-01.md").write_text(body + "[[rules]] · [[index|Tum Projeler]]\n", encoding="utf-8")
    vault.refresh_all()
    text = (project / "daily" / "2026-09-01.md").read_text(encoding="utf-8")
    assert text.startswith(body)  # content untouched (even a [[rules]] inside the text)
    assert "[[old-eeeeeeeeeeeeeeee/old|old]]" in text and "[[rules]] · [[index" not in text
    assert "/rules|" not in text.split("---")[-1]  # no rules.md yet: no link to a missing note


def test_moved_project_page_follows_the_new_name(env: Path) -> None:
    import vault
    project = _project(env, "proj-ffffffffffffffff", "/work/old-name", "2026-10-01", "x")
    vault.refresh_all()
    assert (project / "old-name.md").exists()
    _project(env, "proj-ffffffffffffffff", "/work/new-name")
    vault.refresh_all()
    assert (project / "new-name.md").exists() and not (project / "old-name.md").exists()
    assert (project / "rules.md").exists() is False  # nothing else was created or removed


def test_obsidian_settings_are_merged_not_replaced(env: Path) -> None:
    import vault
    app = env / "projects" / ".obsidian" / "app.json"
    vault.refresh_all()
    assert not app.exists()  # vault never opened in Obsidian: nothing created
    app.parent.mkdir(parents=True)
    app.write_text('{"userIgnoreFilters": ["entries"], "showLineNumber": true}', encoding="utf-8")
    vault.refresh_all()
    data = json.loads(app.read_text(encoding="utf-8"))
    assert data["showLineNumber"] is True and data["userIgnoreFilters"][0] == "entries"
    assert "_merged" in data["userIgnoreFilters"] and "state" in data["userIgnoreFilters"]
    app.write_text("{broken", encoding="utf-8")
    vault.refresh_all()
    assert app.read_text(encoding="utf-8") == "{broken"


def test_candidates_never_point_to_the_generated_page(env: Path) -> None:
    import vault
    project = _project(env, "x-1111111111111111", "/work/x", "2026-10-01", "s")
    text = (project / "candidates.md").read_text(encoding="utf-8")
    assert "`x-1111111111111111/rules.md`" in text and "[[x-1111111111111111/x|" not in text
    (project / "rules.md").write_text("r", encoding="utf-8")
    vault.refresh_all()
    assert "[[x-1111111111111111/rules|x" in (project / "candidates.md").read_text(encoding="utf-8")


def test_obsidian_explorer_shows_readable_names(env: Path) -> None:
    import vault
    _project(env, "app-aaaaaaaaaaaaaaaa", "/work/clients/app", "2026-10-01", "a")
    _project(env, "app-bbbbbbbbbbbbbbbb", '/work/my "x"/app', "2026-10-01", "b")
    settings = env / "projects" / ".obsidian"
    settings.mkdir()
    (settings / "appearance.json").write_text('{"theme": "obsidian"}', encoding="utf-8")
    vault.refresh_all()
    css = (settings / "snippets" / "ai-memory.css").read_text(encoding="utf-8")
    assert '[data-path="app-aaaaaaaaaaaaaaaa"]' in css and 'content: "app (clients)"' in css
    assert 'content: "app (my \\"x\\")"' in css  # quotes escaped
    data = json.loads((settings / "appearance.json").read_text(encoding="utf-8"))
    assert data == {"theme": "obsidian", "enabledCssSnippets": ["ai-memory"]}


def test_user_notes_are_never_overwritten_or_deleted(env: Path) -> None:
    import vault
    project = _project(env, "app-1212121212121212", "/work/app", "2026-10-01", "s")
    for f in project.glob("*.md"):
        if vault._is_ours(f, "project", project.name):
            f.unlink()
    (project / "app.md").write_text("my own note about app", encoding="utf-8")  # same name as the page
    (project / "notes.md").write_text("---\ntitle: x\n---\nexample: ai-memory-page: project\n", encoding="utf-8")
    (project / "rules.md").write_text("---\nai-memory-page: project\nproject_id: app-1212121212121212\n---\nrules",
                                      encoding="utf-8")
    (env / "projects" / "Home.md").write_text("my own home", encoding="utf-8")
    vault.refresh_all()
    assert (project / "app.md").read_text(encoding="utf-8") == "my own note about app"
    assert (project / "notes.md").exists() and (project / "rules.md").exists()
    assert vault._is_ours(project / "app (ai-memory).md", "project", project.name)
    assert (env / "projects" / "Home.md").read_text(encoding="utf-8") == "my own home"
    assert (env / "projects" / "Home (ai-memory).md").exists()
    assert _resolve_links(env / "projects") == []


def test_only_our_exact_footer_is_replaced(env: Path) -> None:
    import vault
    project = _project(env, "doc-3434343434343434", "/work/doc")
    (project / "daily").mkdir()
    mine = "# day\n\nsee [[doc-3434343434343434/daily/2026-09-01|yesterday]]\n"
    (project / "daily" / "2026-09-02.md").write_text(mine, encoding="utf-8")
    vault.refresh_all()
    assert (project / "daily" / "2026-09-02.md").read_text(encoding="utf-8") == mine


def test_names_stay_unique_and_valid(env: Path) -> None:
    import vault
    ids = ["app-aaaaaa1234567890", "app-bbbbbb1234567890", "app (x)-cccccccccccccccc", "con-dddddddddddddddd"]
    for pid, path in zip(ids, ("/x/app", "/x/app", "/y/app (x)", "/z/con")):
        _project(env, pid, path)
    names = vault.display_names(ids)
    assert len({n.casefold() for n in names.values()}) == len(ids)
    assert vault.page_file_name("con") == "con (project).md" and vault.page_file_name("a\x01b" * 60).endswith(".md")
    assert len(vault.page_file_name("😀" * 100).encode("utf-8")) <= 123  # Linux limit is bytes
    assert all(c not in name for name in names.values() for c in "[]|")  # safe inside [[...|label]]


def test_foreign_snippet_is_kept_and_session_start_never_waits(env: Path) -> None:
    import locks
    import vault
    settings = env / "projects" / ".obsidian" / "snippets"
    settings.mkdir(parents=True)
    (settings / "ai-memory.css").write_text("/* mine */", encoding="utf-8")
    vault.refresh_all()
    assert (settings / "ai-memory.css").read_text(encoding="utf-8") == "/* mine */"
    project = env / "projects" / "new-5656565656565656"
    (project / "state").mkdir(parents=True)
    assert locks.acquire(env / "projects" / "_vault.lock", 120)
    started = time.time()
    vault.update(project)
    assert time.time() - started < 1  # busy: skipped at once, the sweep catches up


def test_indented_front_matter_is_not_ownership(env: Path) -> None:
    import vault
    project = _project(env, "app-7878787878787878", "/work/app")
    note = project / "example.md"
    note.write_text("---\ndescription: |\n  ai-memory-page: project\n  project_id: app-7878787878787878\n---\nx",
                    encoding="utf-8")
    vault.refresh_all()
    assert note.exists() and not vault._is_ours(note, "project", project.name)


def test_same_named_project_at_session_start_breaks_no_link(env: Path) -> None:
    import vault
    first = _project(env, "app-9090909090909090", "/work/one/app", "2026-10-01", "s")
    vault.refresh_all()
    assert (first / "app.md").exists()
    second = _project(env, "app-9191919191919191", "/work/two/app")
    vault.update(second)  # quick path: no footer rewrite yet
    assert (first / "app (one).md").exists() and (first / "app.md").exists()
    assert _resolve_links(env / "projects") == []
    vault.refresh_all()  # footers now point to the new name; the old page goes
    assert not (first / "app.md").exists() and _resolve_links(env / "projects") == []


def test_no_link_to_a_users_note_when_no_page_can_be_written(env: Path) -> None:
    import vault
    project = _project(env, "app-9292929292929292", "/work/app")
    (project / "app.md").write_text("mine", encoding="utf-8")
    (project / "app (ai-memory).md").write_text("mine too", encoding="utf-8")
    vault.refresh_all()
    home = (env / "projects" / "Home.md").read_text(encoding="utf-8")
    assert "[[app-9292929292929292/app" not in home and "app" in home


def test_old_page_is_kept_while_the_project_is_busy(env: Path) -> None:
    import locks
    import vault
    first = _project(env, "app-9393939393939393", "/work/one/app", "2026-10-01", "s")
    vault.refresh_all()
    _project(env, "app-9494949494949494", "/work/two/app")
    assert locks.acquire(first / "state" / "daily.lock", 120)  # a summary is writing right now
    vault.refresh_all()
    assert (first / "app.md").exists() and _resolve_links(env / "projects") == []
    locks.release(first / "state" / "daily.lock", force=True)
    vault.refresh_all()
    assert not (first / "app.md").exists() and _resolve_links(env / "projects") == []


def test_home_table_links_do_not_split_columns(env: Path) -> None:
    import vault
    _project(env, "web-5757575757575757", "/work/web", "2026-10-01", "s")
    vault.refresh_all()
    row = next(line for line in (env / "projects" / "Home.md").read_text(encoding="utf-8").splitlines()
               if "web-5757575757575757" in line)
    assert row.replace("\\|", "").count("|") == 5  # 4 columns


def test_user_navigation_line_is_not_taken_for_our_footer(env: Path) -> None:
    import vault
    project = _project(env, "nav-5858585858585858", "/work/nav")
    (project / "rules.md").write_text("r", encoding="utf-8")
    (project / "daily").mkdir()
    mine = ("# day\n\n[[nav-5858585858585858/daily/2026-10-01|Previous day]] · "
            "[[nav-5858585858585858/rules|Rules]]\n")
    (project / "daily" / "2026-10-02.md").write_text(mine, encoding="utf-8")
    vault.refresh_all()
    assert (project / "daily" / "2026-10-02.md").read_text(encoding="utf-8") == mine


def test_unreadable_daily_keeps_the_old_page(env: Path, monkeypatch) -> None:
    import vault
    first = _project(env, "app-5959595959595959", "/work/one/app", "2026-10-01", "s")
    vault.refresh_all()
    _project(env, "app-6060606060606060", "/work/two/app")
    real = vault._read_raw

    def flaky(path: Path) -> str:
        if path.name == "2026-10-01.md":
            raise PermissionError("open in another program")
        return real(path)
    monkeypatch.setattr(vault, "_read_raw", flaky)
    vault.refresh_all()
    assert (first / "app.md").exists()  # the daily note still links to it
    monkeypatch.setattr(vault, "_read_raw", real)
    vault.refresh_all()
    assert not (first / "app.md").exists() and _resolve_links(env / "projects") == []


def test_session_start_hands_the_new_page_to_a_background_process(env: Path, monkeypatch) -> None:
    import bg
    import session_start
    calls = []
    monkeypatch.setattr(bg, "spawn_detached", lambda args, log: calls.append(args))
    data = env / "projects" / "fresh-6161616161616161"
    (data / "state").mkdir(parents=True)
    session_start._ensure_project_page(data, "/work/fresh")
    assert calls and calls[0][2:] == ["--new-project", str(data), "/work/fresh"]
    assert not list(data.glob("*.md"))  # nothing written in the session's own process


def test_only_marked_footers_are_rewritten(env: Path) -> None:
    import vault
    project = _project(env, "meet-6262626262626262", "/work/meet", "2026-10-01", "s")
    daily = project / "daily" / "2026-10-01.md"
    assert daily.read_text(encoding="utf-8").rstrip().endswith(vault.FOOTER_MARK)
    mine = "# day\n\n[[meet-6262626262626262/Toplanti|Toplanti]] · [[Home|Home]]\n"
    (project / "daily" / "2026-10-02.md").write_text(mine, encoding="utf-8")
    vault.refresh_all()
    assert (project / "daily" / "2026-10-02.md").read_text(encoding="utf-8") == mine
