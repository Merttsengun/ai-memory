"""Zero-maintenance behaviour: killed jobs cannot loop forever, a session start does not
fire every waiting job at once, and the scheduler warning ignores time the PC slept."""

from __future__ import annotations

import importlib
import json
import os
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture
def scripts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "memory"
    (home / "projects").mkdir(parents=True)
    monkeypatch.setenv("AI_MEMORY_HOME", str(home))
    monkeypatch.delenv("CLAUDECODE", raising=False)
    if str(REPO / "scripts") not in sys.path:
        sys.path.insert(0, str(REPO / "scripts"))
    for module in ("config", "redaction", "codex_common", "codex_start", "health_report"):
        if module in sys.modules:
            importlib.reload(sys.modules[module])
    return home


def _age(path: Path, seconds: float) -> None:
    old = time.time() - seconds
    os.utime(path, (old, old))


def test_killed_job_is_requeued_then_failed(scripts: Path) -> None:
    import codex_common
    state = scripts / "projects" / "p-0000000000000000" / "state"
    state.mkdir(parents=True)
    job = state / "codex-hookin-abc.json"
    job.write_text(json.dumps({"cwd": "x"}), encoding="utf-8")
    for round_ in range(1, codex_common.MAX_ORPHANED + 1):
        running = job.with_name(job.name + ".running-999")
        job.replace(running)
        _age(running, 2 * 3600)  # the summarizer died over an hour ago
        codex_common.requeue_orphans(state, time.time())
        if round_ < codex_common.MAX_ORPHANED:
            assert json.loads(job.read_text(encoding="utf-8"))["_orphaned"] == round_
    assert not job.exists() and (state / "failed" / job.name).exists()
    assert "killed-repeatedly" in (state / "codex-health.json").read_text(encoding="utf-8")


def test_unreadable_or_stranded_orphans(scripts: Path) -> None:
    import codex_common
    state = scripts / "projects" / "p-0000000000000000" / "state"
    state.mkdir(parents=True)
    broken = state / "codex-hookin-bad.json.running-1"
    broken.write_text('{"cwd": "x", "sess', encoding="utf-8")  # cut mid-write
    _age(broken, 2 * 3600)
    stranded = state / "codex-hookin-mid.json.requeue-999999"  # a recovery died between its two renames
    stranded.write_text(json.dumps({"cwd": "x", "_orphaned": 1}), encoding="utf-8")
    _age(stranded, 3600)
    (state / "codex-hookin-mid.json.requeue-999999.tmp").write_text("{}", encoding="utf-8")  # its dead temp copy
    codex_common.requeue_orphans(state, time.time())
    assert (state / "failed" / "codex-hookin-bad.json").exists()  # visible, not requeued empty
    assert json.loads((state / "codex-hookin-mid.json").read_text(encoding="utf-8"))["_orphaned"] == 2
    assert not list(state.glob("*.requeue-*")) and not list(state.glob("*.running-*"))


def test_running_job_is_not_requeued(scripts: Path) -> None:
    import codex_common
    state = scripts / "projects" / "p-0000000000000000" / "state"
    state.mkdir(parents=True)
    running = state / "codex-hookin-abc.json.running-999"
    running.write_text("{}", encoding="utf-8")  # fresh mtime: alive
    codex_common.requeue_orphans(state, time.time())
    assert running.exists()


def test_session_start_runs_only_the_oldest_job(scripts: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import codex_start
    state = scripts / "projects" / "p-0000000000000000" / "state"
    state.mkdir(parents=True)
    for name, age in (("b", 3000), ("a", 9000), ("c", 1000)):
        job = state / f"codex-hookin-{name}.json"
        job.write_text("{}", encoding="utf-8")
        _age(job, age)
    started = []
    monkeypatch.setattr(codex_start.subprocess, "Popen", lambda args, **_: started.append(args[-1]))
    codex_start.recover_stale(state.parent)
    assert [Path(p).name for p in started] == ["codex-hookin-a.json"]


def test_scheduler_age_ignores_sleep_and_reboot(scripts: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import health_report as h
    now = 1_000_000.0
    # Up 10 h since boot, awake 2 h of it: 8 h asleep.
    monkeypatch.setattr(h, "_uptime_seconds", lambda: 10 * 3600)
    monkeypatch.setattr(h, "awake_seconds", lambda: 2 * 3600)
    asleep_now = 8 * 3600
    # Sweep 8 h 15 min ago, then the PC slept 8 h: 15 min awake.
    assert h._awake_since(now - (8 * 3600 + 900), asleep_now - 8 * 3600, now) == 900
    # No sleep since the sweep (counter rounding may go back a second): plain wall time.
    assert h._awake_since(now - 3 * 3600, asleep_now + 1, now) == 3 * 3600
    # Sweep before this boot: only this boot's awake time counts.
    assert h._awake_since(now - 20 * 3600, 123, now) == 2 * 3600
    # Old log line without uyku=: the old behaviour.
    assert h._awake_since(now - 3600, None, now) == 3600
