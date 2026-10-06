#!/usr/bin/env python3
"""Fold one project memory folder into another.

Why (2026-09-28): ids used to come from the folder path only, so a moved
folder (~/code/my-app -> ~/work/my-app) or a nested
checkout (my-app vs my-app/my-app) silently started a second, empty
memory and the old history stopped being injected.

Two modes:
  --auto <project-path>   fold the legacy path-based folder of this path
                          into its current id (project_id.py v1.3). Called
                          from the SessionStart hooks; a no-op when there is
                          nothing to fold.
  --from <id> --into <id> explicit merge (e.g. a non-git folder that moved,
                          or a wrapper folder around the real project).

Both write projects/aliases.json (old -> new) so the old id keeps
resolving to the new one. Nothing is deleted: the source folder is first
claimed by an atomic rename into projects/_merged/ (whoever wins the
rename does the work, no fcntl), its immutable entries are moved into the
target, and the rest stays there as an archive. rules.md/candidates.md are
hand-written, so they are never rewritten: if both sides have content the
source copy is kept next to the target as <name>.merged-from-<id>.md.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
import project_id as pid_mod  # noqa: E402
import render_daily  # noqa: E402

PROJECTS_ROOT = pid_mod.PROJECTS_ROOT
MERGED_ROOT = PROJECTS_ROOT / "_merged"
ID_RE = re.compile(r"^[a-z0-9-]+-g?[0-9a-f]{15,16}$")
HAND_WRITTEN = ("rules.md",)
PENDING_GLOBS = ("hookin-*.json", "codex-hookin-*.json")
INPROGRESS = ".inprogress-into-"
LOCK_DIR = PROJECTS_ROOT / ".aliases.lock"
LOCK_WAIT_SECONDS = 5
LOCK_STALE_SECONDS = 60
RESUME_AFTER_SECONDS = 600


def _has_content(folder: Path) -> bool:
    if any((folder / "entries").glob("*/*.json")):
        return True
    for name in HAND_WRITTEN:
        f = folder / name
        if f.is_file() and f.stat().st_size > 0:
            return True
    return any(any((folder / "state").glob(g)) for g in PENDING_GLOBS)


def _write_alias(src_id: str, dst_id: str) -> None:
    # Read-modify-write under a mkdir lock (atomic on Windows too), so two
    # sessions folding different projects at once cannot drop each other's alias.
    deadline = time.time() + LOCK_WAIT_SECONDS
    while True:
        try:
            LOCK_DIR.mkdir()
            break
        except FileExistsError:
            try:
                if time.time() - LOCK_DIR.stat().st_mtime > LOCK_STALE_SECONDS:
                    LOCK_DIR.rmdir()
                    continue
            except OSError:
                pass
            if time.time() > deadline:
                raise OSError("aliases.json kilidi alinamadi")
            time.sleep(0.1)
    try:
        aliases = pid_mod.load_aliases()
        if pid_mod.resolve_alias(dst_id, aliases) == src_id:
            raise ValueError(f"alias dongusu olusurdu: {src_id} -> {dst_id}")
        aliases[src_id] = dst_id
        tmp = pid_mod.ALIASES_FILE.with_name(f".aliases.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(aliases, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, pid_mod.ALIASES_FILE)
    finally:
        try:
            LOCK_DIR.rmdir()
        except OSError:
            pass


def _unique(target: Path) -> Path:
    n = 1
    candidate = target
    while candidate.exists():
        candidate = target.with_name(f"{target.stem}-m{n}{target.suffix}")
        n += 1
    return candidate


def merge(src_id: str, dst_id: str, *, dry_run: bool = False) -> list[str]:
    """Returns human-readable log lines. Raises ValueError on bad input."""
    for value in (src_id, dst_id):
        if not ID_RE.match(value):
            raise ValueError(f"gecersiz proje id: {value}")
    dst_id = pid_mod.resolve_alias(dst_id, pid_mod.load_aliases())
    if src_id == dst_id:
        raise ValueError("kaynak ve hedef ayni (ya da hedef zaten kaynaga yonleniyor)")
    src = PROJECTS_ROOT / src_id
    dst = PROJECTS_ROOT / dst_id
    if not src.is_dir():
        return [f"kaynak yok, atlandi: {src_id}"]

    entries = sorted((src / "entries").glob("*/*.json"))
    log = [f"{src_id} -> {dst_id}: {len(entries)} ozet"]
    if dry_run:
        return log + ["(dry-run, hicbir sey degismedi)"]

    MERGED_ROOT.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    claimed = MERGED_ROOT / f"{src_id}-{stamp}-{os.getpid()}{INPROGRESS}{dst_id}"
    try:
        os.rename(src, claimed)
    except OSError as error:
        return log + [f"kaynak alinamadi (baska bir surec mi?): {error}"]
    return log + _finish(claimed, src_id, dst_id)


def _merge_progress(src_state: Path, dst_state: Path) -> None:
    """Move "how far was each session summarized" along with the jobs; otherwise a
    moved job would summarize its session from the start and duplicate old entries.

    checkpoint-<session>.json: per session; if both sides have one, the further one wins.
    done.txt / codex-done.txt: line logs read "last line wins", so the target's own
    (newer) lines are kept after the source's. Re-running after a crash is harmless."""
    for ckpt in src_state.glob("checkpoint-*.json"):
        target = dst_state / ckpt.name
        if target.exists():
            def turns(path: Path) -> int:
                try:
                    return int(json.loads(path.read_text(encoding="utf-8")).get("processed_turns") or 0)
                except (OSError, ValueError, AttributeError):
                    return -1
            if turns(ckpt) <= turns(target):
                ckpt.unlink()
                continue
        os.replace(ckpt, target)
    for name in ("done.txt", "codex-done.txt"):
        src_file = src_state / name
        if not src_file.is_file():
            continue
        dst_file = dst_state / name
        merged = src_file.read_text(encoding="utf-8")
        if dst_file.is_file():
            merged = merged.rstrip("\n") + "\n" + dst_file.read_text(encoding="utf-8")
        tmp = dst_file.with_name(f".{name}.{os.getpid()}.tmp")
        tmp.write_text(merged, encoding="utf-8")
        os.replace(tmp, dst_file)
        src_file.unlink()


def _finish(claimed: Path, src_id: str, dst_id: str) -> list[str]:
    """Idempotent: safe to re-run on a claimed folder after a crash."""
    dst = PROJECTS_ROOT / dst_id
    log: list[str] = []
    # daily/ stays in the archive, so its file names also cover dates whose
    # entries were already moved before a crash (keeps _finish re-runnable).
    dates: set[str] = {f.stem for f in (claimed / "daily").glob("*.md")}
    for entry in sorted((claimed / "entries").glob("*/*.json")):
        date_dir = dst / "entries" / entry.parent.name
        date_dir.mkdir(parents=True, exist_ok=True)
        target = date_dir / entry.name
        if target.is_file() and target.read_bytes() == entry.read_bytes():
            entry.unlink()  # already moved by an earlier (interrupted) run
        else:
            os.replace(entry, _unique(target))
        dates.add(entry.parent.name)

    state_dst = dst / "state"
    state_dst.mkdir(parents=True, exist_ok=True)
    for pattern in PENDING_GLOBS:
        for pending in (claimed / "state").glob(pattern):
            os.replace(pending, _unique(state_dst / pending.name))
    _merge_progress(claimed / "state", state_dst)

    for name in HAND_WRITTEN:
        src_file = claimed / name
        if not src_file.is_file() or src_file.stat().st_size == 0:
            continue
        dst_file = dst / name
        if not dst_file.exists() or dst_file.stat().st_size == 0:
            os.replace(src_file, dst_file)
            log.append(f"{name} tasindi")
        else:
            keep = _unique(dst / f"{Path(name).stem}.merged-from-{src_id}.md")
            os.replace(src_file, keep)
            log.append(f"{name} iki tarafta da dolu, elle birlestir: {keep.name}")

    # Any target date with entries but no daily note (a crash between the
    # move and the render) is rendered too, so nothing is left invisible.
    for date_dir in (dst / "entries").glob("*"):
        if date_dir.is_dir() and not (dst / "daily" / f"{date_dir.name}.md").exists():
            dates.add(date_dir.name)
    for date in sorted(dates):
        content = render_daily.render(dst / "entries", date)
        daily_dir = dst / "daily"
        daily_dir.mkdir(parents=True, exist_ok=True)
        target = daily_dir / f"{date}.md"
        tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
        tmp.write_text(content, encoding="utf-8")
        os.replace(tmp, target)

    render_daily.write_candidates(dst)
    _write_alias(src_id, dst_id)
    done = claimed.with_name(claimed.name.split(INPROGRESS)[0])
    os.rename(claimed, done)
    log.append(f"arsiv: _merged/{done.name}")
    return log


def resume_interrupted() -> list[str]:
    """Finish merges whose process died after claiming the source folder."""
    log: list[str] = []
    if not MERGED_ROOT.is_dir():
        return log
    for claimed in MERGED_ROOT.glob(f"*{INPROGRESS}*"):
        try:
            prefix, rest = claimed.name.split(INPROGRESS, 1)
            dst_id, _, resumed = rest.partition(".r")
            if resumed:
                # Another session is resuming it; only take over if that one
                # also died (its resume stamp is old).
                resumed_at = dt.datetime.strptime(resumed, "%Y%m%d%H%M%S").timestamp()
                if time.time() - resumed_at < RESUME_AFTER_SECONDS:
                    continue
            src_id, day, clock, _pid = prefix.rsplit("-", 3)
            # Age comes from the claim stamp in the name: a rename keeps the
            # folder's old mtime, so mtime would let us steal a live merge.
            claimed_at = dt.datetime.strptime(f"{day}-{clock}", "%Y%m%d-%H%M%S").timestamp()
            if time.time() - claimed_at < RESUME_AFTER_SECONDS:
                continue
            if not (ID_RE.match(src_id) and ID_RE.match(dst_id)):
                continue
            # Re-claim by rename so two sessions never resume the same folder.
            taken = claimed.with_name(f"{prefix}{INPROGRESS}{dst_id}.r{dt.datetime.now():%Y%m%d%H%M%S}")
            os.rename(claimed, taken)
            log += [f"yarim kalan birlestirme tamamlaniyor: {src_id} -> {dst_id}"] + _finish(taken, src_id, dst_id)
        except (OSError, ValueError):
            continue
    return log


def auto(project_path: str, *, dry_run: bool = False, current: str | None = None, resume: bool = True) -> list[str]:
    # The start hook passes resume=False; the background sweep finishes partial merges.
    if resume and not dry_run:
        resume_interrupted()
    # Reuse the identity if the caller already computed it (saves 2 more git calls).
    current = current or pid_mod.project_id(project_path)
    legacy = pid_mod.legacy_id(project_path)
    if legacy == current:
        return []
    src = PROJECTS_ROOT / legacy
    if not src.is_dir():
        return []
    if not _has_content(src):
        if dry_run:
            return [f"bos eski klasor kaldirilacak: {legacy}"]
        try:
            MERGED_ROOT.mkdir(parents=True, exist_ok=True)
            os.rename(src, MERGED_ROOT / f"{legacy}-empty-{os.getpid()}")
        except OSError:
            return []
        _write_alias(legacy, current)
        return [f"bos eski klasor arsivlendi: {legacy}"]
    return merge(legacy, current, dry_run=dry_run)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--auto", metavar="PROJECT_PATH")
    parser.add_argument("--from", dest="src")
    parser.add_argument("--into", dest="dst")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    try:
        if args.auto:
            lines = auto(args.auto, dry_run=args.dry_run)
        elif args.src and args.dst:
            lines = merge(args.src, args.dst, dry_run=args.dry_run)
        else:
            parser.error("--auto PATH ya da --from ID --into ID gerekli")
            return 2
    except ValueError as error:
        print(f"hata: {error}", file=sys.stderr)
        return 1
    for line in lines:
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
