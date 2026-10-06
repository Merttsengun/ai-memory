#!/usr/bin/env python3
"""Move orphaned Claude Code native memory to a relocated project's new path.

Claude Code stores native memory under ~/.claude/projects/<name>/memory/; <name>
is derived from the project path. Moving the project folder leaves notes in the
old <name> folder orphaned. At SessionStart, this module:

  1. Computes this session's memory folder using Claude Code's own rule
     (verified against claude.exe 2.1.287):
       root = canonical git root (main repo for a worktree), else project root
       name = root.replace(/[^a-zA-Z0-9]/g, "-")   (per UTF-16 code unit)
       if name > 200: name[:200] + "-" + base36(abs(java_hash(root)))
  2. Orphan: folder with at least one .md whose original path, read from transcript
     cwd and producing the exact name, is absent on disk. Skip unreadable folders.
  3. Candidate: the orphan's original path and the current root have the same final
     folder name (case-insensitive).
  4. One candidate + no conflicts: backup -> copy -> verify -> merge MEMORY.md
     -> verify -> delete old .md files. Multiple candidates / conflicts: only warn
     (once per situation).

Never raises (run_hook); logs errors and returns nothing.
For testing: NATIVE_MIGRATE_ROOT (projects folder), NATIVE_MIGRATE_BACKUP
(backup/log folder) environment variables or function parameters.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import shutil
import sys
import time
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from texts import t  # noqa: E402

MAX_NAME = 200
JSONL_HEAD_BYTES = 64 * 1024     # In real data, cwd appears within roughly the first 10 KB.
JSONL_PER_DIR = 300              # Wide enough to detect ambiguity (two paths with the same name)
MAX_MIGRATE_BYTES = 2 * 1024 * 1024  # Do not automatically migrate note sets larger than this.
TIME_BUDGET = 4.0          # Seconds; exit without changes if exceeded.
LOCK_STALE = 120           # Seconds
MEMORY_CTX_BYTES = 6000
CWD_RE = re.compile(r'"cwd"\s*:\s*"((?:[^"\\]|\\.)*)"')
WIN = os.name == "nt"


# --------------------------------------------------------------------------- path rule
def _java_hash(s: str) -> int:
    h = 0
    for unit in _utf16_units(s):
        h = (h * 31 + unit) & 0xFFFFFFFF
    return h - 0x100000000 if h & 0x80000000 else h


def _utf16_units(s: str):
    data = s.encode("utf-16-le", errors="surrogatepass")
    for i in range(0, len(data), 2):
        yield data[i] | (data[i + 1] << 8)


def _base36(n: int) -> str:
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    if n == 0:
        return "0"
    out = ""
    while n:
        n, r = divmod(n, 36)
        out = digits[r] + out
    return out


def sanitize(path: str) -> str:
    """Claude Code Vx(): replace each UTF-16 code unit outside [a-zA-Z0-9] with '-'."""
    out = []
    for unit in _utf16_units(path):
        c = chr(unit) if unit < 128 else ""
        out.append(c if c.isascii() and c.isalnum() else "-")
    name = "".join(out)
    if len(name) <= MAX_NAME:
        return name
    return f"{name[:MAX_NAME]}-{_base36(abs(_java_hash(path)))}"


def _same_name(a: str, b: str) -> bool:
    return a.casefold() == b.casefold() if WIN else a == b


def _norm_cmp(p: str) -> str:
    p = unicodedata.normalize("NFC", os.path.normpath(p))
    return p.casefold() if WIN else p


# --------------------------------------------------------------------------- git root
def _read_text(p: Path, limit: int = 4096) -> str | None:
    try:
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            return f.read(limit)
    except OSError:
        return None


def _canonical_root(root: str) -> str:
    """Claude Code Be()/tn(): if .git is a file (worktree), resolve the main repo root."""
    git = Path(root, ".git")
    if not git.is_file():
        return root
    text = (_read_text(git) or "").strip()
    if not text.startswith("gitdir:"):
        return root
    gitdir = Path(root, text[7:].strip()).resolve() if not os.path.isabs(text[7:].strip()) else Path(text[7:].strip())
    common = _read_text(gitdir / "commondir")
    if common is None:
        return root
    common = common.strip()
    cdir = Path(common) if os.path.isabs(common) else (gitdir / common)
    cdir = Path(os.path.normpath(str(cdir)))
    if os.path.normcase(str(gitdir.parent)) != os.path.normcase(str(cdir / "worktrees")):
        return root
    back = _read_text(gitdir / "gitdir")
    if back is None:
        return root
    back_p = Path(back.strip()) if os.path.isabs(back.strip()) else gitdir / back.strip()
    if os.path.normcase(os.path.normpath(str(back_p))) != os.path.normcase(os.path.normpath(str(Path(root, ".git")))):
        return root
    if cdir.name != ".git":
        return root  # Bare repo: Claude treats this specially too; stay on the safe side.
    return os.path.normpath(str(cdir.parent))


def memory_root_for(project_dir: str) -> str:
    """Path Claude Code uses for its memory key (canonical git root or project path)."""
    cur = os.path.normpath(project_dir)
    while True:
        g = os.path.join(cur, ".git")
        try:
            if os.path.lexists(g) and not os.path.islink(g) and (os.path.isdir(g) or os.path.isfile(g)):
                return _canonical_root(cur)
        except OSError:
            pass
        parent = os.path.dirname(cur)
        if parent == cur:
            return os.path.normpath(project_dir)
        cur = parent


# --------------------------------------------------------------------------- helpers
class Ctx:
    def __init__(self, projects_root: Path, backup_root: Path, dry_run: bool, budget: float = TIME_BUDGET):
        self.budget = budget
        self.projects_root = projects_root
        self.backup_root = backup_root
        self.dry_run = dry_run
        self.t0 = time.monotonic()
        self.log_lines: list[str] = []

    def log(self, msg: str) -> None:
        self.log_lines.append(msg)

    def over_budget(self) -> bool:
        return time.monotonic() - self.t0 > self.budget

    def flush_log(self) -> None:
        if not self.log_lines or self.dry_run:
            return
        try:
            self.backup_root.mkdir(parents=True, exist_ok=True)
            with open(self.backup_root / "migrate.log", "a", encoding="utf-8") as f:
                stamp = dt.datetime.now().isoformat(timespec="seconds")
                for line in self.log_lines:
                    f.write(f"{stamp} {line}\n")
        except OSError:
            pass


def _default_projects_root() -> Path:
    env = os.environ.get("NATIVE_MIGRATE_ROOT")
    if env:
        return Path(env)
    cfg = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(cfg, "projects") if cfg else Path.home() / ".claude" / "projects"


def _default_backup_root(projects_root: Path) -> Path:
    env = os.environ.get("NATIVE_MIGRATE_BACKUP")
    if env:
        return Path(env)
    legacy = projects_root.parent / "memory-yedek-oto"  # older installs: keep their backups + warn-once state
    return legacy if legacy.exists() else projects_root.parent / "ai-memory-native-backup"


def _top_md(mem: Path) -> list[Path]:
    try:
        return sorted(p for p in mem.iterdir() if p.suffix.lower() == ".md" and p.is_file() and not p.is_symlink())
    except OSError:
        return []


def _is_temp_path(p: str) -> bool:
    low = p.replace("/", "\\").casefold()
    return "\\appdata\\local\\temp\\" in low + "\\"


def _cwds_in_dir(d: Path, ctx: Ctx) -> list[str]:
    """Read cwd values only from the start of a few of the folder's newest jsonl files."""
    try:
        files = [p for p in d.iterdir() if p.suffix == ".jsonl" and p.is_file()]
    except OSError:
        return []
    try:
        files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        pass
    found: list[str] = []
    for p in files[:JSONL_PER_DIR]:
        if ctx.over_budget():
            break
        try:
            with open(p, "rb") as f:
                head = f.read(JSONL_HEAD_BYTES).decode("utf-8", errors="replace")
        except OSError:
            continue
        for m in CWD_RE.finditer(head):
            try:
                v = json.loads('"' + m.group(1) + '"')
            except ValueError:
                continue
            if isinstance(v, str) and v and v not in found:
                found.append(v)
    return found


def _jsonl_mtimes(d: Path, exclude_stem: str = "") -> list[float]:
    out = []
    try:
        for e in os.scandir(d):
            if e.name.endswith(".jsonl") and e.name[:-6] != exclude_stem and e.is_file():
                out.append(e.stat().st_mtime)
    except OSError:
        pass
    return out


def original_path(folder: Path, all_names: list[str], ctx: Ctx) -> str | None:
    """Find the path that produces the EXACT folder name from transcript cwd values.

    The cwd itself or a parent folder (session opened in a git subfolder)
    is a candidate; sanitize(candidate) must equal the folder name. Check subfolder
    sessions too, as they may live in separate folders (name + '-...'). Return None
    unless exactly one path is found (no guessing)."""
    name = folder.name
    dirs = [folder] + [folder.parent / n for n in all_names
                       if n != name and n.startswith(name + "-")][:20]
    matches: dict[str, str] = {}
    for d in dirs:
        for cwd in _cwds_in_dir(d, ctx):
            cur = os.path.normpath(cwd)
            while True:
                if sanitize(cur) == name:
                    matches.setdefault(_norm_cmp(cur), cur)
                    break
                parent = os.path.dirname(cur)
                if parent == cur or len(sanitize(parent)) < len(name) - 1:
                    break
                cur = parent
    if ctx.over_budget():
        raise TimeoutError("sure asildi; kismi okumayla karar verilmez")
    if len(matches) == 1:
        return next(iter(matches.values()))
    if len(matches) > 1:
        ctx.log(f"belirsiz orijinal yol, atlandi: {name} -> {sorted(matches.values())}")
    return None


def _path_missing(p: str) -> bool:
    """Is the path absent on disk? A missing drive/network root (unmounted) does not count."""
    drive, _ = os.path.splitdrive(p)
    anchor = (drive + os.sep) if drive else os.sep
    try:
        os.stat(anchor)
    except OSError:
        return False
    try:
        os.stat(p)
        return False
    except (FileNotFoundError, NotADirectoryError):
        return True
    except OSError:
        return False  # Access error etc.: do not treat as absent.


# --------------------------------------------------------------------------- main logic
def find_candidates(project_dir: str, ctx: Ctx, target_name: str) -> list[tuple[Path, str]]:
    base = os.path.basename(os.path.normpath(project_dir))
    if not base:
        return []
    suffix = "-" + sanitize(base)
    try:
        names = sorted(e.name for e in os.scandir(ctx.projects_root) if e.is_dir())
    except OSError:
        return []
    out: list[tuple[Path, str]] = []
    for n in names:
        if ctx.over_budget():
            raise TimeoutError("tarama suresi asildi")
        if _same_name(n, target_name) or "-AppData-Local-Temp-" in n:
            continue
        if not (n.casefold().endswith(suffix.casefold()) or len(n) > MAX_NAME):
            continue
        folder = ctx.projects_root / n
        if not _top_md(folder / "memory"):
            continue
        orig = original_path(folder, names, ctx)
        if orig is None or _is_temp_path(orig):
            continue
        if not _same_name(os.path.basename(orig), base):
            continue
        if not _path_missing(orig):
            continue
        out.append((folder, orig))
    return out


def scan_all(ctx: Ctx) -> list[tuple[str, str, int]]:
    """Dry-run report: all orphans (across projects)."""
    names = sorted(e.name for e in os.scandir(ctx.projects_root) if e.is_dir())
    res = []
    for n in names:
        if "-AppData-Local-Temp-" in n:
            continue
        mds = _top_md(ctx.projects_root / n / "memory")
        if not mds:
            continue
        orig = original_path(ctx.projects_root / n, names, ctx)
        if orig is None:
            res.append((n, "(orijinal yol okunamadi - atlanir)", len(mds)))
        elif _path_missing(orig):
            res.append((n, f"YETIM: {orig}", len(mds)))
    return res


def _merge_memory_index(src_text: str, dst_text: str) -> tuple[str, list[str]]:
    have = {l.strip() for l in dst_text.splitlines() if l.strip()}
    add = []
    for l in src_text.splitlines():
        s = l.strip()
        if s and s not in have:
            add.append(l.rstrip())
            have.add(s)
    if not add:
        return dst_text, []
    base = dst_text if (not dst_text or dst_text.endswith("\n")) else dst_text + "\n"
    return base + "\n".join(add) + "\n", add


def _write_atomic_new(dst: Path, data: bytes) -> None:
    """Create dst; fail if it exists. Use tmp + rename (Windows rename never overwrites)."""
    tmp = dst.with_name(dst.name + f".migrate-tmp-{os.getpid()}")
    with open(tmp, "xb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    try:
        if dst.exists():
            raise FileExistsError(str(dst))
        os.rename(tmp, dst)
    finally:
        if tmp.exists():
            tmp.unlink()


def _write_atomic_replace(dst: Path, data: bytes) -> None:
    tmp = dst.with_name(dst.name + f".migrate-tmp-{os.getpid()}")
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, dst)


class _Lock:
    def __init__(self, path: Path):
        self.path = path
        self.ok = False

    def __enter__(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            try:
                if time.time() - self.path.stat().st_mtime > LOCK_STALE:
                    # Atomic rename: only one process can remove the same stale lock.
                    junk = self.path.with_name(f"{self.path.name}.stale-{os.getpid()}-{time.time_ns()}")
                    os.rename(self.path, junk)
                    if time.time() - junk.stat().st_mtime > LOCK_STALE:
                        os.unlink(junk)
                    else:  # Picked up another process's fresh lock in the meantime: restore it and abort.
                        try:
                            os.rename(junk, self.path)
                        except OSError:
                            os.unlink(junk)
                        return self
            except OSError:
                pass
            self.token = f"{os.getpid()}-{time.time_ns()}"
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, self.token.encode())
            os.close(fd)
            self.ok = True
        except OSError:
            self.ok = False
        return self

    def __exit__(self, *a):
        if self.ok:
            try:
                if self.path.read_text() == self.token:
                    self.path.unlink()
            except OSError:
                pass


def _warn_once(ctx: Ctx, key: str, signature: str) -> bool:
    """Warn once per situation. True: warn this time."""
    state_p = ctx.backup_root / "warned.json"
    try:
        state = json.loads(state_p.read_text(encoding="utf-8"))
        if not isinstance(state, dict):
            state = {}
    except (OSError, ValueError):
        state = {}
    if state.get(key) == signature:
        return False
    if ctx.dry_run:
        return True
    state[key] = signature
    try:
        ctx.backup_root.mkdir(parents=True, exist_ok=True)
        _write_atomic_replace(state_p, json.dumps(state, ensure_ascii=False, indent=1).encode("utf-8"))
    except OSError:
        pass
    return True


def migrate(src_folder: Path, orig: str, target_mem: Path, ctx: Ctx, project_dir: str) -> str:
    src_mem = src_folder / "memory"
    srcs = _top_md(src_mem)
    if not srcs:
        return ""
    proj = os.path.basename(os.path.normpath(project_dir))
    total = sum(s.stat().st_size for s in srcs)
    if total > MAX_MIGRATE_BYTES:
        ctx.log(f"COK BUYUK ({total} bayt), tasinmadi: {src_folder.name}")
        if _warn_once(ctx, target_mem.parent.name, f"big:{src_folder.name}"):
            return t("n_too_big", proj=proj, orig=orig, src=src_mem)
        return ""
    # Snapshot: use these bytes throughout; compare the source again before deleting.
    snap = {s: s.read_bytes() for s in srcs}
    src_index = next((s for s in srcs if s.name.casefold() == "memory.md"), None)
    tgt_index = target_mem / "MEMORY.md"
    try:  # Stop if MEMORY.md files are not valid UTF-8 (lossless merging is not guaranteed).
        if src_index is not None:
            snap[src_index].decode("utf-8")
            if tgt_index.exists():
                tgt_index.read_bytes().decode("utf-8")
    except UnicodeDecodeError:
        ctx.log(f"MEMORY.md gecerli UTF-8 degil, tasinmadi: {src_folder.name}")
        return ""
    # Conflict analysis (identical files indicate a previous partial migration, not a conflict)
    conflicts, identical, to_copy = [], [], []
    for s in srcs:
        if s is src_index:
            continue
        d = target_mem / s.name
        if d.exists():
            try:
                same = d.read_bytes() == snap[s]
            except OSError:
                same = False
            (identical if same else conflicts).append(s)
        else:
            to_copy.append(s)
    if conflicts:
        names = ", ".join(sorted(c.name for c in conflicts))
        ctx.log(f"CAKISMA, tasinmadi: {src_folder.name} -> {target_mem.parent.name}: {names}")
        sig = "conflict:" + src_folder.name + ":" + names
        if _warn_once(ctx, target_mem.parent.name, sig):
            return t("n_conflict", proj=proj, orig=orig, names=names, src=src_mem)
        return ""
    if ctx.dry_run:
        return f"[dry-run] {len(srcs)} not taşınacaktı: {src_mem} -> {target_mem}"

    # 1) Backup (from the snapshot) + the previous destination MEMORY.md
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    bdir = ctx.backup_root / stamp / src_folder.name
    i = 1
    while bdir.exists():
        bdir = ctx.backup_root / f"{stamp}-{i}" / src_folder.name
        i += 1
    bdir.mkdir(parents=True)
    for s, data in snap.items():
        (bdir / s.name).write_bytes(data)
        if (bdir / s.name).read_bytes() != data:
            raise OSError(f"yedek dogrulanamadi: {s}")
    dst_before = tgt_index.read_bytes() if tgt_index.exists() else None
    if dst_before is not None:
        (bdir / "_hedef_MEMORY.md.onceki").write_bytes(dst_before)
    (bdir / "_manifest.json").write_text(json.dumps({
        "zaman": stamp, "kaynak": str(src_mem), "orijinal_yol": orig,
        "hedef": str(target_mem), "proje": project_dir,
        "dosyalar": [s.name for s in srcs]}, ensure_ascii=False, indent=1), encoding="utf-8")

    # 2) Copy + verify
    target_mem.mkdir(parents=True, exist_ok=True)
    verified = list(identical)
    for s in to_copy:
        d = target_mem / s.name
        _write_atomic_new(d, snap[s])
        if d.read_bytes() != snap[s]:
            raise OSError(f"kopya dogrulanamadi: {d}")
        verified.append(s)

    # 3) Merge MEMORY.md + verify
    merged_text = None
    if src_index is not None:
        src_text = snap[src_index].decode("utf-8")
        if dst_before is not None:
            dst_text = dst_before.decode("utf-8")
            new_text, _ = _merge_memory_index(src_text, dst_text)
            if new_text != dst_text:
                if tgt_index.read_bytes() != dst_before:   # Do not overwrite changes made by another writer in the meantime.
                    raise OSError("hedef MEMORY.md tasima sirasinda degisti")
                _write_atomic_replace(tgt_index, new_text.encode("utf-8"))
        else:
            _write_atomic_new(tgt_index, snap[src_index])
        merged_text = tgt_index.read_bytes().decode("utf-8")
        have = {l.strip() for l in merged_text.splitlines()}
        if any(l.strip() and l.strip() not in have for l in src_text.splitlines()):
            raise OSError("MEMORY.md birlestirmesi dogrulanamadi")
        verified.append(src_index)

    # 4) Only now delete source .md files: only those verified AND still unchanged.
    kept = []
    for s in verified:
        try:
            if s.read_bytes() != snap[s]:
                kept.append(s.name)
                ctx.log(f"kaynak tasima sirasinda degisti, silinmedi: {s}")
                continue
            s.unlink()
        except OSError as e:
            kept.append(s.name)
            ctx.log(f"silinemedi (kopyasi hedefte var): {s}: {e}")
    ctx.log(f"TASINDI: {len(srcs)} not {src_folder.name} -> {target_mem.parent.name} (yedek: {bdir})")

    msg = t("n_moved", n=len(srcs), proj=proj, orig=orig, backup=bdir)
    if kept:
        msg += t("n_kept", names=", ".join(kept), src=src_mem)
    if merged_text is None and tgt_index.exists():
        merged_text = tgt_index.read_bytes().decode("utf-8", errors="replace")
    if merged_text:
        enc = merged_text.encode("utf-8")
        body = enc[:MEMORY_CTX_BYTES].decode("utf-8", errors="ignore")
        if len(enc) > MEMORY_CTX_BYTES:
            body += t("n_truncated", path=tgt_index)
        msg += "\n\n" + t("n_context", dst=target_mem) + "\n" + body
    return msg


def _settings_override_memory(dirs: list[str], projects_root: Path) -> bool:
    if os.environ.get("CLAUDE_COWORK_MEMORY_PATH_OVERRIDE") or os.environ.get("CLAUDE_CODE_REMOTE_MEMORY_DIR"):
        return True
    paths = [projects_root.parent / "settings.json"]
    for d in dirs:
        paths += [Path(d, ".claude", "settings.local.json"), Path(d, ".claude", "settings.json")]
    for p in paths:
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and data.get("autoMemoryDirectory"):
            return True
    return False


def run(project_dir: str, hook: dict | None = None, projects_root: Path | None = None,
        backup_root: Path | None = None, dry_run: bool = False) -> str:
    hook = hook if isinstance(hook, dict) else {}
    projects_root = Path(projects_root) if projects_root else _default_projects_root()
    backup_root = Path(backup_root) if backup_root else _default_backup_root(projects_root)
    ctx = Ctx(projects_root, backup_root, dry_run)
    try:
        if not isinstance(project_dir, str) or not project_dir or not os.path.isabs(project_dir):
            return ""
        project_dir = os.path.normpath(project_dir)
        if not os.path.isdir(project_dir) or not projects_root.is_dir():
            return ""
        home = os.path.normpath(str(Path.home()))
        if _norm_cmp(project_dir) == _norm_cmp(home) or os.path.dirname(project_dir) == project_dir \
                or _is_temp_path(project_dir):
            return ""
        root = memory_root_for(project_dir)
        if _settings_override_memory([project_dir, root], projects_root):
            return ""
        target_name = sanitize(root)
        # Self-check: transcript folder derives from cwd; outside git, it must match the memory name.
        tp = hook.get("transcript_path")
        if isinstance(tp, str) and tp and _norm_cmp(root) == _norm_cmp(project_dir):
            tdir = os.path.basename(os.path.dirname(tp))
            if not _same_name(tdir, target_name):
                ctx.log(f"oz-denetim tutmadi, atlandi: hesap={target_name} transcript={tdir}")
                return ""
        target_mem = projects_root / target_name / "memory"
        cands = find_candidates(root, ctx, target_name)
        if not cands:
            return ""
        if len(cands) > 1:
            listing = "; ".join(f"{o}" for _, o in cands)
            ctx.log(f"BIRDEN FAZLA ADAY, tasinmadi: {target_name}: {listing}")
            sig = "multi:" + "|".join(sorted(f.name for f, _ in cands))
            if _warn_once(ctx, target_name, sig):
                return t("n_ambiguous", name=os.path.basename(root), listing=listing,
                         cmd=", ".join(str(f / "memory") for f, _ in cands))
            return ""
        folder, orig = cands[0]
        # If the new location was used BEFORE the old location's last use, this may be
        # a different project with the same name: skip migration and warn.
        old_dirs = [folder] + [projects_root / n for n in os.listdir(projects_root)
                               if n != folder.name and n.startswith(folder.name + "-")]
        old_last = max([m for d in old_dirs for m in _jsonl_mtimes(d)] or [0.0])
        sid = str(hook.get("session_id") or "")
        new_dirs = {projects_root / target_name}
        if isinstance(tp, str) and tp:
            new_dirs.add(Path(os.path.dirname(tp)))
        new_first = min([m for d in new_dirs for m in _jsonl_mtimes(d, sid)] or [float("inf")])
        if new_first < old_last:
            ctx.log(f"ZAMAN CELISKISI, tasinmadi: {folder.name} (son {old_last}) -> {target_name} (ilk {new_first})")
            if _warn_once(ctx, target_name, "overlap:" + folder.name):
                return t("n_reused", name=os.path.basename(root), orig=orig, path=folder / "memory")
            return ""
        if dry_run:
            return migrate(folder, orig, target_mem, ctx, root)
        with _Lock(backup_root / ".migrate.lock") as lk:
            if not lk.ok:
                ctx.log("kilit alinamadi (baska oturum tasiyor olabilir), atlandi")
                return ""
            # Recheck after locking: another session may have just migrated it.
            if not _top_md(folder / "memory"):
                return ""
            return migrate(folder, orig, target_mem, ctx, root)
    except Exception as e:  # noqa: BLE001 - the hook must never break
        ctx.log(f"HATA ({type(e).__name__}): {e} [proje={project_dir}]")
        return ""
    finally:
        ctx.flush_log()


def run_hook(project_dir: str, hook: dict | None = None) -> str:
    try:
        return run(project_dir, hook)
    except Exception:  # noqa: BLE001
        return ""


def main(argv: list[str]) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Native hafiza tasima (SessionStart yardimcisi)")
    ap.add_argument("--cwd", help="proje yolu")
    ap.add_argument("--root", help="projects klasoru (varsayilan ~/.claude/projects)")
    ap.add_argument("--backup", help="yedek/log klasoru")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--scan-all", action="store_true", help="tum yetimleri listele (salt okunur)")
    a = ap.parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass
    root = Path(a.root) if a.root else _default_projects_root()
    if a.scan_all:
        ctx = Ctx(root, Path(a.backup) if a.backup else _default_backup_root(root), True, budget=600)
        for n, info, cnt in scan_all(ctx):
            print(f"{n}  md={cnt}  {info}")
        print(f"# sure: {time.monotonic() - ctx.t0:.2f} sn")
        return 0
    out = run(a.cwd or os.getcwd(), {}, root, Path(a.backup) if a.backup else None, a.dry_run)
    print(out or "(bir sey yapilmadi)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
