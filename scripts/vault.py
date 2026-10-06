#!/usr/bin/env python3
"""Readable Obsidian navigation for the memory folder (projects/ is the vault).

Generated, never hand-edited:
  - projects/<Home>.md            the home page: projects by last activity, global rules,
                                  left-out projects, and a separate technical/archive part
  - projects/<id>/<Name>.md       one page per project: folder, rules, rule candidates,
                                  latest daily notes with readable dates

Project folders keep their ids (paths and ids never change here). Readable names come
from the recorded project path (index.json, aliases.json); two projects with the same
folder name are told apart by their parent folder, then by a short id. Every link uses
the full path inside the vault plus a readable label, so it can never resolve to another
project's file (a bare [[rules]] did, with 7 rules.md files in the vault).

Pages are rewritten only when their content changes. update(project_dir) runs after every
summary (render_daily) and for a new project (session start); refresh_all() runs on every
scheduled sweep and can be called by hand:  python vault.py
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config import PROJECTS_ROOT, is_excluded  # noqa: E402
from texts import t  # noqa: E402

PAGE_MARK = "ai-memory-page"   # frontmatter key on every generated page
ACTIVE_DAYS = 30
RECENT_DAYS = 10
ID_RE = re.compile(r"^(?P<slug>.+)-(?P<hash>g?[0-9a-f]{15,16})$")
UNSAFE = re.compile(r'[<>:"/\\|?*#^\[\]]')


# ---------------------------------------------------------------- names / data
def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def project_dirs() -> list[Path]:
    if not PROJECTS_ROOT.is_dir():
        return []
    return sorted(p for p in PROJECTS_ROOT.iterdir()
                  if p.is_dir() and not p.name.startswith(("_", ".")) and ID_RE.match(p.name))


def known_paths() -> dict[str, str]:
    """project id -> its folder path, from index.json; ids that replaced an older id
    (aliases.json: old -> new) inherit the old id's path when they have none."""
    index = {k: v for k, v in _read_json(PROJECTS_ROOT / "index.json").items() if isinstance(v, str)}
    paths = dict(index)
    for old, new in _read_json(PROJECTS_ROOT / "aliases.json").items():
        if isinstance(new, str) and new not in paths and old in index:
            paths[new] = index[old]
    return paths


def display_names(ids: list[str]) -> dict[str, str]:
    """Readable, UNIQUE name per project id."""
    paths = known_paths()

    def base(pid: str) -> str:
        path = paths.get(pid)
        if path:
            name = os.path.basename(os.path.normpath(path)) or path
        else:
            match = ID_RE.match(pid)
            name = match.group("slug") if match else pid
        return _label_safe(name)

    names = {pid: base(pid) for pid in ids}
    groups: dict[str, list[str]] = {}
    for pid, name in names.items():
        groups.setdefault(name.casefold(), []).append(pid)
    for members in groups.values():
        if len(members) < 2:
            continue
        for pid in members:  # same folder name: add the parent folder
            parent = os.path.basename(os.path.dirname(os.path.normpath(paths.get(pid, ""))))
            if parent:
                names[pid] = f"{names[pid]} ({_label_safe(parent)})"
    # Still the same (same parent, no parent known, or clashing with another project's
    # plain name): add part of the id, longer each round, finally the whole id.
    for width in (6, 10, 0):
        clashing = _clashing(ids, names)
        if not clashing:
            return names
        for pid in clashing:
            match = ID_RE.match(pid)
            tag = (match.group("hash") if match else pid)[-width:] if width else pid
            names[pid] = f"{names[pid]} ({tag})" if width else f"{base(pid)} ({tag})"
    for _ in range(len(ids)):  # last resort: the id itself (ids are unique)
        clashing = _clashing(ids, names)
        if not clashing:
            break
        for pid in clashing:
            names[pid] = pid
    return names


def _clashing(ids: list[str], names: dict[str, str]) -> list[str]:
    counts: dict[str, int] = {}
    for pid in ids:
        counts[names[pid].casefold()] = counts.get(names[pid].casefold(), 0) + 1
    return [pid for pid in ids if counts[names[pid].casefold()] > 1]


def _label_safe(text: str) -> str:
    """Usable as a wikilink label: [ ] | would end or split the link."""
    return text.replace("[", "(").replace("]", ")").replace("|", "-").replace("\n", " ")


RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
OWN_FILES = {"rules", "candidates", "index"}  # the project's own notes: never a page name


def page_file_name(name: str) -> str:
    """A file name that is valid on Windows, macOS and Linux."""
    base = re.sub(r"[\x00-\x1f]", "", UNSAFE.sub("-", name)).strip(" .")
    base = base.encode("utf-8")[:120].decode("utf-8", "ignore").strip(" .") or "project"
    if base.casefold() in OWN_FILES or base.split(".")[0].casefold() in RESERVED:
        base += " (project)"
    return base + ".md"


def name_of(pid: str) -> str:
    ids = [p.name for p in project_dirs()]
    if pid not in ids:
        ids.append(pid)
    return display_names(ids).get(pid) or pid


def _daily_dates(project: Path) -> list[str]:
    days = []
    for path in (project / "daily").glob("????-??-??.md"):
        try:
            dt.date.fromisoformat(path.stem)
        except ValueError:
            continue
        days.append(path.stem)
    return sorted(days, reverse=True)


def _entries(project: Path) -> int:
    return sum(1 for _ in (project / "entries").glob("*/*.json"))


def _open_candidates(project: Path) -> int:
    try:
        text = (project / "candidates.md").read_text(encoding="utf-8")
    except OSError:
        return 0
    return sum(1 for line in text.splitlines() if re.match(r"^- \d{4}-\d{2}-\d{2}: ", line))


def _first_summary_line(daily: Path) -> str:
    try:
        lines = daily.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    seen_heading = False
    for line in lines:
        if line.startswith("## "):
            seen_heading = True
            continue
        if seen_heading and line.strip() and not line.startswith(("**", "- ", "---", "[[")):
            text = line.strip()
            return text if len(text) <= 110 else text[:107].rstrip() + "…"
    return ""


def readable_date(day: str) -> str:
    try:
        date = dt.date.fromisoformat(day)
    except ValueError:
        return day
    months = t("v_months").split()
    return t("v_date", day=date.day, month=months[date.month - 1], year=date.year)


# ------------------------------------------------------------------- links
def _front_matter(path: Path) -> dict[str, str]:
    """key: value lines of a leading --- block (only what we need, no YAML parser)."""
    try:
        with path.open(encoding="utf-8") as fh:
            head = fh.read(1000)
    except OSError:
        return {}
    lines = head.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    data = {}
    for line in lines[1:]:
        if line.strip() == "---":
            return data
        if line[:1].isspace():
            continue  # inside a block value (e.g. "description: |"): not a key
        key, sep, value = line.partition(":")
        if sep:
            data[key.strip()] = value.strip()
    return {}  # unterminated block: not ours


def _is_ours(path: Path, kind: str, pid: str = "") -> bool:
    """A page this module generated: its front matter names the page kind (and, for a
    project page, THIS project's id). A user's note never matches by accident."""
    meta = _front_matter(path)
    return meta.get(PAGE_MARK) == kind and (not pid or meta.get("project_id") == pid)


def _free_target(folder: Path, file_name: str, kind: str, pid: str = "") -> Path | None:
    """Where to write a generated page: the wanted name if it is free or already ours,
    else '<name> (ai-memory).md'. None when both are taken by the user's own notes."""
    for candidate in (file_name, file_name[:-3] + " (ai-memory).md"):
        path = folder / candidate
        if not path.exists() or _is_ours(path, kind, pid):
            return path
    return None


def home_target() -> Path | None:
    return _free_target(PROJECTS_ROOT, f"{t('v_home_file')}.md", "home")


def home_link() -> str:
    target = home_target()
    return f"[[{target.stem}|{t('v_home_file')}]]" if target else t("v_home_file")


def project_target(pid: str, name: str) -> Path | None:
    return _free_target(PROJECTS_ROOT / pid, page_file_name(name), "project", pid)


def project_page_link(pid: str, name: str) -> str:
    target = project_target(pid, name)
    return f"[[{pid}/{target.stem}|{name}]]" if target else name


def rules_link(pid: str, name: str) -> str:
    return f"[[{pid}/rules|{t('v_rules_of', name=name)}]]"


def footer_for(pid: str, name: str) -> str:
    """Links at the bottom of a daily note: project page, its rules (only if they exist,
    so no link points at a missing note) and home."""
    parts = [project_page_link(pid, name)]
    if (PROJECTS_ROOT / pid / "rules.md").is_file():
        parts.append(rules_link(pid, name))
    parts.append(home_link())
    return " · ".join(parts)


def daily_footer(pid: str) -> str:
    return footer_for(pid, name_of(pid))


def rules_or_page_link(pid: str) -> str:
    """Where to put promoted rules: a link to the rules note if it exists, else its path as
    plain text (never the generated project page, which is overwritten)."""
    if (PROJECTS_ROOT / pid / "rules.md").is_file():
        return rules_link(pid, name_of(pid))
    return f"`{pid}/rules.md`"


# ------------------------------------------------------------------- pages
def _read_raw(path: Path) -> str:
    """Text exactly as stored (no newline translation)."""
    with path.open(encoding="utf-8", newline="") as fh:
        return fh.read()


def _write_if_changed(path: Path, content: str) -> bool:
    """Writes `content` byte-for-byte (newline=""), only when it differs."""
    try:
        if _read_raw(path) == content:
            return False
    except OSError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with tmp.open("w", encoding="utf-8", newline="") as fh:
        fh.write(content)
    os.replace(tmp, path)
    return True


def render_project_page(project: Path, name: str, path: str | None) -> str:
    pid = project.name
    days = _daily_dates(project)
    lines = [
        "---",
        f"{PAGE_MARK}: project",
        f"project_id: {pid}",
        f"aliases: [{json.dumps(name, ensure_ascii=False)}]",
        "---",
        f"# {name}",
        "",
        f"_{t('v_generated')}_",
        "",
        f"- **{t('v_folder')}:** `{path}`" if path else f"- **{t('v_folder')}:** {t('v_unknown_path')}",
        f"- **{t('v_id')}:** `{pid}`",
        f"- **{t('v_col_last')}:** {readable_date(days[0]) if days else '—'} · "
        f"**{t('v_col_entries')}:** {_entries(project)} · "
        + t("v_status", pending=len(list((project / 'state').glob('*hookin-*.json'))),
            failed=len(list((project / 'state' / 'failed').glob('*.json')))),
        "",
        f"## {t('v_rules')}",
        "",
        rules_link(pid, name) if (project / "rules.md").is_file() else t("v_rules_none"),
    ]
    if (project / "candidates.md").is_file():
        lines += ["", f"## {t('v_candidates')}", "",
                  f"[[{pid}/candidates|{t('v_candidates')}]] · {t('v_candidates_open', n=_open_candidates(project))}"]
    lines += ["", f"## {t('v_recent')}", ""]
    if not days:
        lines.append(t("v_no_days"))
    for day in days[:RECENT_DAYS]:
        preview = _first_summary_line(project / "daily" / f"{day}.md")
        lines.append(f"- [[{pid}/daily/{day}|{readable_date(day)}]]" + (f" — {preview}" if preview else ""))
    if len(days) > RECENT_DAYS:
        lines += ["", f"### {t('v_older_days', n=len(days) - RECENT_DAYS)}", ""]
        lines += [f"- [[{pid}/daily/{day}|{readable_date(day)}]]" for day in days[RECENT_DAYS:]]
    lines += ["", "---", f"← {home_link()}", ""]
    return "\n".join(lines)


def _project_row(project: Path, name: str) -> tuple[str, str]:
    days = _daily_dates(project)
    last = days[0] if days else ""
    row = (f"| {project_page_link(project.name, name)} | {readable_date(last) if last else '—'} | "
           f"{_entries(project)} | {t('v_yes') if (project / 'rules.md').is_file() else t('v_no')} |")
    return last, row


def render_home() -> str:
    projects = project_dirs()
    names = display_names([p.name for p in projects])
    paths = known_paths()
    today = dt.date.today()
    active, older, excluded, empty = [], [], [], []
    for project in projects:
        name = names[project.name]
        has_notes = any(project.glob("daily/*.md")) or (project / "rules.md").is_file()
        if is_excluded(paths.get(project.name), project.name):
            excluded.append((name, project))
        elif not has_notes:
            empty.append((name, project))
        else:
            last, row = _project_row(project, name)
            try:
                recent = last and (today - dt.date.fromisoformat(last)).days <= ACTIVE_DAYS
            except ValueError:
                recent = False
            (active if recent else older).append((last, name.casefold(), row))
    header = (f"| {t('v_col_project')} | {t('v_col_last')} | {t('v_col_entries')} | {t('v_col_rules')} |\n"
              "|---|---|---|---|")
    lines = [
        "---",
        f"{PAGE_MARK}: home",
        "---",
        f"# {t('v_home_title')}",
        "",
        f"_{t('v_generated')}_",
        "",
    ]
    if (PROJECTS_ROOT / "rules.md").is_file():
        lines += [f"**[[rules|{t('v_global_rules')}]]**", ""]
    for title, rows in ((t("v_active", days=ACTIVE_DAYS), active), (t("v_older"), older)):
        if not rows:
            continue
        rows.sort(key=lambda r: (r[0], r[1]), reverse=True)
        lines += [f"## {title}", "", header, *[r[2] for r in rows], ""]
    if excluded:
        lines += [f"## {t('v_excluded')}", ""]
        lines += [f"- {project_page_link(p.name, name)}" for name, p in sorted(excluded, key=lambda x: x[0].casefold())]
        lines.append("")
    if empty:  # e.g. a project opened for the first time: listed (by name) until it has notes
        lines += [f"## {t('v_empty_folders')}", "",
                  ", ".join(project_page_link(p.name, name)
                            for name, p in sorted(empty, key=lambda x: x[0].casefold())), ""]
    lines += [f"## {t('v_technical')}", "", t("v_technical_note"), ""]
    if (PROJECTS_ROOT / "index.md").is_file():
        lines.append(f"- [[index|{t('v_old_overview')}]]")
    lines.append("")
    return "\n".join(lines)


def _sync_project_page(project: Path, names: dict[str, str], paths: dict[str, str],
                       remove_old: bool = True) -> bool:
    pid = project.name
    name = names.get(pid) or pid
    target = project_target(pid, name)
    if target is None:
        return False  # both names are the user's own notes: leave them alone
    changed = _write_if_changed(target, render_project_page(project, name, paths.get(pid)))
    for other in project.glob("*.md") if remove_old else ():  # a renamed project's old page
        if other != target and other.stem.casefold() not in OWN_FILES and _is_ours(other, "project", pid):
            other.unlink(missing_ok=True)
            changed = True
    return changed


# Footers written by earlier versions: a bare [[rules]] resolves to any of the vault's
# rules.md files. These lines, and footers we wrote ourselves, are rewritten with the
# CURRENT names (a project page is renamed when e.g. a same-named project appears).
# Only that one line changes; the note's content is never touched.
LEGACY_FOOTERS = {
    "[[rules]] · [[index|Tum Projeler]]",
    "[[rules]] · [[index|Tüm Projeler]]",
    "[[rules]] · [[index|All projects]]",
}


def _our_footer(pid: str) -> re.Pattern[str]:
    """Exactly the footer footer_for() writes (any names): project page link, optional
    rules link, home link. A line the user wrote never has this shape by accident."""
    link = r"\[\[[^\[\]|]+\|[^\[\]]+\]\]"
    own = r"\[\[" + re.escape(pid) + r"/[^\[\]|]+\|[^\[\]]+\]\]"
    rules = r"\[\[" + re.escape(pid) + r"/rules\|[^\[\]]+\]\]"
    return re.compile(rf"^{own}(?: · {rules})? · {link}$")


def sync_links(project: Path, name: str) -> int:
    """Footers of daily notes + the rule-candidates page, under the project's daily lock
    (render_daily writes the same files). A busy project is skipped, not waited for."""
    from locks import held
    pid = project.name
    footer = footer_for(pid, name)
    ours = _our_footer(pid)
    fixed = 0
    with held(project / "state" / "daily.lock", stale_seconds=120, wait_seconds=1) as got:
        if not got:
            return 0  # busy: the next refresh will do it
        for daily in project.glob("daily/*.md"):
            try:
                text = _read_raw(daily)  # keep the file's own line endings (LF or CRLF)
            except OSError:
                continue
            lines = text.split("\n")
            idx = max((i for i, line in enumerate(lines) if line.strip()), default=-1)
            last = lines[idx].strip() if idx >= 0 else ""
            if last in LEGACY_FOOTERS or ours.match(last):
                lines[idx] = footer + ("\r" if lines[idx].endswith("\r") else "")
                fixed += _write_if_changed(daily, "\n".join(lines))
        if (project / "candidates.md").is_file():
            import render_daily
            fixed += _write_if_changed(project / "candidates.md", render_daily.render_candidates(project))
    return fixed


TECHNICAL = ("entries", "state", "_merged", "index.json", "aliases.json", "_health.json")


def hide_technical() -> bool:
    """Keep raw records and archives out of Obsidian's search, graph and quick switcher
    (userIgnoreFilters). Only when the vault was opened in Obsidian; other settings kept."""
    app = PROJECTS_ROOT / ".obsidian" / "app.json"
    if not app.parent.is_dir():
        return False
    try:
        data = json.loads(_read_raw(app)) if app.exists() else {}
    except (OSError, ValueError):
        return False  # never overwrite a settings file we cannot read
    if not isinstance(data, dict):
        return False
    filters = data.get("userIgnoreFilters")
    filters = list(filters) if isinstance(filters, list) else []
    missing = [f for f in TECHNICAL if f not in filters]
    if not missing:
        return False
    data["userIgnoreFilters"] = filters + missing
    return _write_if_changed(app, json.dumps(data, indent=2, ensure_ascii=False) + "\n")


SNIPPET = "ai-memory"
SNIPPET_HEADER = "/* Generated by ai-memory (vault.py)"


def _css_string(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ") + '"'


def render_snippet(names: dict[str, str]) -> str:
    """CSS for Obsidian's file explorer: project folders show their readable name instead of
    the id (the folder itself is not renamed), technical folders are hidden."""
    lines = [
        SNIPPET_HEADER + "; rewritten on every refresh. */",
        "/* Technical folders: raw records, job files, merge archive. */",
        '.nav-folder:has(> .nav-folder-title[data-path$="/entries"]),',
        '.nav-folder:has(> .nav-folder-title[data-path$="/state"]),',
        '.nav-folder:has(> .nav-folder-title[data-path="_merged"]) { display: none; }',
        "",
        "/* Project folders: readable names. */",
    ]
    for pid, name in sorted(names.items(), key=lambda kv: kv[1].casefold()):
        sel = f'.nav-folder-title[data-path={_css_string(pid)}] .nav-folder-title-content'
        lines.append(f"{sel} {{ font-size: 0; }}")
        lines.append(f"{sel}::after {{ content: {_css_string(name)}; font-size: var(--nav-item-size, 13px); }}")
    return "\n".join(lines) + "\n"


def obsidian_names(names: dict[str, str]) -> bool:
    """Write the snippet and switch it on (appearance.json, other settings kept).
    Only when the vault was opened in Obsidian."""
    settings = PROJECTS_ROOT / ".obsidian"
    if not settings.is_dir():
        return False
    css = settings / "snippets" / f"{SNIPPET}.css"
    try:
        foreign = css.exists() and not _read_raw(css).startswith(SNIPPET_HEADER)
    except OSError:
        foreign = True
    if foreign:
        return False  # a snippet of the user's with the same name: not ours to overwrite
    changed = _write_if_changed(css, render_snippet(names))
    appearance = settings / "appearance.json"
    try:
        data = json.loads(_read_raw(appearance)) if appearance.exists() else {}
    except (OSError, ValueError):
        return changed  # never overwrite a settings file we cannot read
    if not isinstance(data, dict):
        return changed
    enabled = data.get("enabledCssSnippets")
    enabled = list(enabled) if isinstance(enabled, list) else []
    if SNIPPET not in enabled:
        data["enabledCssSnippets"] = enabled + [SNIPPET]
        changed |= _write_if_changed(appearance, json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    return changed


def _write_home() -> bool:
    target = home_target()
    return _write_if_changed(target, render_home()) if target else False


def update(project: Path) -> None:
    """A new project at session start: its page + the home page, nothing else, and only
    if nobody else is refreshing right now (never makes the session wait)."""
    from locks import held
    with held(PROJECTS_ROOT / "_vault.lock", stale_seconds=120, wait_seconds=0) as got:
        if not got:
            return  # render_daily / the sweep will do it
        projects = project_dirs()
        names = display_names([p.name for p in projects] + ([] if project in projects else [project.name]))
        paths = known_paths()
        # A same-named project renames the others too: write every page under its current
        # name (small files, no per-project locks). Old pages stay until the full refresh
        # has pointed the daily-note footers at the new ones, so no link breaks meanwhile.
        for other in projects + ([project] if project.is_dir() and project not in projects else []):
            if ID_RE.match(other.name):
                _sync_project_page(other, names, paths, remove_old=False)
        _write_home()


def refresh_all() -> int:
    """Every project page, daily-note footer and candidates page + the home page.
    Runs after a summary (in the background) and on every sweep. A project that fails
    (e.g. an unwritable folder) is skipped; the others are still refreshed.
    Returns how many files changed."""
    from locks import held
    with held(PROJECTS_ROOT / "_vault.lock", stale_seconds=120, wait_seconds=20) as got:
        if not got:
            return 0  # another refresh is running; the scheduled sweep catches up
        projects = project_dirs()
        names = display_names([p.name for p in projects])
        paths = known_paths()
        changed = 0
        for project in projects:
            try:
                changed += _sync_project_page(project, names, paths, remove_old=False)
                changed += sync_links(project, names[project.name])
                changed += _sync_project_page(project, names, paths)  # now drop the old page
            except (OSError, ValueError):
                continue
        for step in (_write_home, hide_technical, lambda: obsidian_names(names)):
            try:
                changed += step()
            except (OSError, ValueError):
                pass
        return changed


if __name__ == "__main__":
    started = time.time()
    count = refresh_all()
    print(f"{count} page(s) updated in {time.time() - started:.2f}s")
