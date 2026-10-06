"""Owner-tracked directory lock shared by Claude and Codex.

mkdir is atomic on Windows too. The lock stores the owner PID, process START
time, and a unique token.

Takeover rules:
  - Never take over a lock younger than stale_seconds.
  - Never take over while its owner is alive (same PID + same start time);
    wait for long jobs. The start time distinguishes PID reuse.
  - A separate "<lock>.takeover" lock serializes takeovers: two processes cannot
    take over the same stale lock or delete each other's NEW lock.
    Re-read the lock while holding the takeover lock and confirm
    that it still belongs to the same stale owner.

Note: on Windows, os.kill(pid, 0) TERMINATES the process instead of checking liveness;
use OpenProcess/GetExitCodeProcess/GetProcessTimes to check it instead.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import time
import uuid
from pathlib import Path

TAKEOVER_STALE_SECONDS = 60  # Remove a takeover lock this old (the takeover process died).


# ------------------------------------------------------------- process identity
def _win_process(pid: int):
    import ctypes
    from ctypes import wintypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.GetProcessTimes.argtypes = (wintypes.HANDLE,) + (ctypes.POINTER(wintypes.FILETIME),) * 4
    kernel32.GetProcessTimes.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    return ctypes, wintypes, kernel32


def process_identity(pid: int) -> tuple[bool, int]:
    """(alive, start time). Start time is 0 if unknown."""
    if pid <= 0:
        return False, 0
    if os.name == "nt":
        ctypes, wintypes, kernel32 = _win_process(pid)
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            # Access denied = process exists under another user; otherwise it does not exist.
            return ctypes.get_last_error() == 5, 0
        try:
            code = wintypes.DWORD()
            if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)) and code.value != 259:
                return False, 0  # Not STILL_ACTIVE: exited
            created, exited, kern, user = (wintypes.FILETIME() for _ in range(4))
            started = 0
            if kernel32.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited),
                                        ctypes.byref(kern), ctypes.byref(user)):
                started = (created.dwHighDateTime << 32) | created.dwLowDateTime
            return True, started
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False, 0
    except PermissionError:
        return True, 0
    try:  # Linux: /proc/<pid>/stat field 22 = start time (ticks)
        return True, int(Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19])
    except (OSError, ValueError, IndexError):
        return True, 0


def pid_alive(pid: int) -> bool:
    return process_identity(pid)[0]


_SELF_START = None


def _self_start() -> int:
    global _SELF_START
    if _SELF_START is None:
        _SELF_START = process_identity(os.getpid())[1]
    return _SELF_START


# --------------------------------------------------------------------- lock
def _owner(lock: Path) -> dict:
    try:
        data = json.loads((lock / "owner.json").read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except (OSError, ValueError):
        pass
    try:  # Died before writing owner details / legacy lock format
        return {"pid": 0, "ts": lock.stat().st_mtime, "token": ""}
    except OSError:
        return {}


def _owner_running(owner: dict) -> bool:
    alive, started = process_identity(int(owner.get("pid") or 0))
    if not alive:
        return False
    recorded = int(owner.get("start") or 0)
    # Known but different start times mean the PID was reused by another process.
    return not (recorded and started and recorded != started)


def acquire(lock: Path, stale_seconds: float) -> bool:
    """Acquire the lock or return False. Safely take over stale locks whose owners died."""
    token = uuid.uuid4().hex
    try:
        lock.parent.mkdir(parents=True, exist_ok=True)  # a project's state/ may not exist yet
        lock.mkdir()
    except FileExistsError:
        if not _take_over(lock, stale_seconds):
            return False
    except OSError:
        return False
    try:
        (lock / "owner.json").write_text(json.dumps({
            "pid": os.getpid(), "start": _self_start(), "ts": time.time(), "token": token}),
            encoding="utf-8")
    except OSError:
        pass
    return True


def _take_over(lock: Path, stale_seconds: float) -> bool:
    """Serialize stale lock takeover. On success, the lock directory is recreated as OURS
    (the caller writes owner.json)."""
    first = _owner(lock)
    if not first or time.time() - float(first.get("ts") or 0) < stale_seconds or _owner_running(first):
        return False
    gate = lock.with_name(lock.name + ".takeover")
    try:
        gate.mkdir()
    except FileExistsError:
        try:
            if time.time() - gate.stat().st_mtime > TAKEOVER_STALE_SECONDS:
                gate.rmdir()  # Takeover process died; retry on the next attempt.
        except OSError:
            pass
        return False
    except OSError:
        return False
    try:
        # Re-read INSIDE the gate: leave any new lock created by another takeover alone.
        again = _owner(lock)
        if again != first:
            return False
        # Move the stale lock aside in ONE step, then delete it. On Windows a file that
        # another process is reading at that moment cannot be removed, so an in-place
        # rmtree could stop halfway and leave the lock behind; a rename is all-or-nothing
        # and is simply retried. While the stale lock exists nobody else can create one,
        # so the only thing we can move is that stale lock.
        tomb = lock.with_name(f"{lock.name}.stale-{uuid.uuid4().hex[:8]}")
        for _ in range(20):
            try:
                os.rename(lock, tomb)
                break
            except FileNotFoundError:
                break
            except OSError:
                time.sleep(0.05)
        else:
            return False
        shutil.rmtree(tomb, ignore_errors=True)
        try:
            lock.mkdir()
        except OSError:
            return False  # a plain acquire() got there first: it is the single winner
        return True
    finally:
        try:
            gate.rmdir()
        except OSError:
            pass


def release(lock: Path, force: bool = False) -> None:
    """Release only our own lock (force: manual cleanup)."""
    if not force:
        owner = _owner(lock)
        if owner.get("pid") not in (None, 0, os.getpid()):
            return  # Another process took over; do not delete its lock.
    shutil.rmtree(lock, ignore_errors=True)


@contextlib.contextmanager
def held(lock: Path, stale_seconds: float, wait_seconds: float = 30.0):
    """Wait for the lock for short critical sections (daily/index writes).
    If acquisition fails, return got=False; the caller MUST NOT WRITE."""
    deadline = time.time() + wait_seconds
    got = acquire(lock, stale_seconds)
    while not got and time.time() < deadline:
        time.sleep(0.2)
        got = acquire(lock, stale_seconds)
    try:
        yield got
    finally:
        if got:
            release(lock)
