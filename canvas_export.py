#!/usr/bin/env python3
"""Export all active Canvas courses to local Markdown + files.

Produces <export-dir>/<COURSE>/ with index.md, assignments.md, announcements.md,
modules.md, pages.md and a files/ folder of downloaded course materials — a local,
grep-able mirror other tooling (or an LLM agent) can read to help with coursework.

Auth: reuses canvas_session.txt (written by auto_login.py). Idempotent/incremental:
files are skipped when a same-size copy already exists. Each course is isolated in a
try/except so one failure doesn't abort the rest.
"""
import json
import os
import re
import datetime
import urllib.request
import urllib.parse
from pathlib import Path
from html.parser import HTMLParser

BASE_DIR = Path(os.environ.get("CANVAS_CHECK_HOME", Path.home() / "canvas-check"))
COOKIE = (BASE_DIR / "canvas_session.txt").read_text().strip()
OUT = Path(os.environ.get("CANVAS_EXPORT_DIR", Path.home() / "canvas-export"))
CANVAS_BASE_URL = os.environ.get("CANVAS_BASE_URL", "https://canvas.instructure.com").rstrip("/")
API = CANVAS_BASE_URL + "/api/v1"
HEADERS = {"Cookie": f"canvas_session={COOKIE}", "Accept": "application/json"}
MAX_FILE_BYTES = 150 * 1024 * 1024  # skip > 150 MB

now = datetime.datetime.now(datetime.timezone.utc)


def fetch(path, params=None):
    url = path if path.startswith("http") else f"{API}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params, doseq=True)
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except Exception:
        return None


def fetch_all(path, params=None):
    """Follow Link rel=next pagination, return concatenated list."""
    params = dict(params or {})
    params.setdefault("per_page", 100)
    url = f"{API}{path}?" + urllib.parse.urlencode(params, doseq=True)
    out = []
    while url:
        req = urllib.request.Request(url, headers=HEADERS)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                data = json.loads(r.read())
                link = r.headers.get("Link", "")
        except Exception:
            break
        if not isinstance(data, list):
            break
        out.extend(data)
        url = None
        for part in link.split(","):
            if 'rel="next"' in part:
                m = re.search(r"<([^>]+)>", part)
                if m:
                    url = m.group(1)
                break
    return out


class _Stripper(HTMLParser):
    def __init__(self):
        super().__init__()
        self.t = []

    def handle_data(self, d):
        self.t.append(d)

    def handle_starttag(self, tag, attrs):
        if tag in ("p", "br", "div", "li", "tr"):
            self.t.append("\n")


def strip_html(h):
    if not h:
        return ""
    p = _Stripper()
    p.feed(h)
    text = "".join(p.t)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def short_label(course_code, name):
    base = (course_code or name or "").split("/")[0].strip()
    m = re.search(r"([A-Z]{2,})-?(\d+[A-Z]*)", base)
    if m:
        return f"{m.group(1)} {m.group(2)}"
    return (course_code or name or "course").strip()


def safe_dir(s):
    return re.sub(r"[^A-Za-z0-9 _-]", "", s).strip().replace(" ", "_") or "course"


def safe_file(s):
    return re.sub(r"[^A-Za-z0-9 ._-]", "_", s).strip() or "file"


def write(path, text):
    path.write_text(text, encoding="utf-8")


def download_file(url, dest):
    if not url:
        return "no-url"
    if dest.exists() and dest.stat().st_size > 0:
        return "skip-exists"
    req = urllib.request.Request(url, headers={"Cookie": HEADERS["Cookie"]})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            clen = int(r.headers.get("Content-Length") or 0)
            if clen and clen > MAX_FILE_BYTES:
                return f"skip-large({clen // 1024 // 1024}MB)"
            data = r.read(MAX_FILE_BYTES + 1)
            if len(data) > MAX_FILE_BYTES:
                return "skip-large"
            dest.write_bytes(data)
            return f"ok({len(data) // 1024}KB)"
    except Exception as e:
        return f"fail({e})"


# ---- export ---------------------------------------------------------------

OUT.mkdir(parents=True, exist_ok=True)
courses = fetch("/courses", {"enrollment_state": "active", "per_page": 50}) or []
courses = [c for c in courses if c.get("id") and not c.get("access_restricted_by_date")]

all_assignments = []  # aggregate digest across courses

index_lines = [f"# Canvas Coursework Export\n\nExported: {now.astimezone().strftime('%Y-%m-%d %H:%M %Z')}",
               f"\n{len(courses)} active course(s). Each folder holds index.md, assignments.md, "
               "announcements.md, modules.md, pages.md and a files/ directory.\n"]

for c in courses:
    cid = c["id"]
    label = short_label(c.get("course_code"), c.get("name"))
    cdir = OUT / safe_dir(label)
    summary = {"assignments": 0, "announcements": 0, "modules": 0, "pages": 0, "files": 0}
    try:
        cdir.mkdir(parents=True, exist_ok=True)
        (cdir / "files").mkdir(exist_ok=True)
        curl = f"{CANVAS_BASE_URL}/courses/{cid}"

        # index.md (meta + syllabus)
        full = fetch(f"/courses/{cid}", {"include[]": "syllabus_body"}) or {}
        idx = [f"# {full.get('name', label)}",
               f"- Code: {full.get('course_code', '')}",
               f"- Course ID: {cid}",
               f"- URL: {curl}", ""]
        syl = strip_html(full.get("syllabus_body", ""))
        if syl:
            idx += ["## Syllabus", syl, ""]
        desc = strip_html(full.get("public_description", ""))
        if desc:
            idx += ["## Description", desc, ""]
        write(cdir / "index.md", "\n".join(idx))

        # assignments.md
        assigns = fetch_all(f"/courses/{cid}/assignments") or []
        alines = [f"# Assignments — {label}\n"]
        for a in assigns:
            aid = a.get("id")
            sub = fetch(f"/courses/{cid}/assignments/{aid}/submissions/self") or {}
            status = sub.get("workflow_state", "unknown")
            alines.append(f"## {a.get('name', 'Untitled')}")
            alines.append(f"- Due: {a.get('due_at') or 'no due date'}")
            alines.append(f"- Points: {a.get('points_possible') or 0}")
            alines.append(f"- Status: {status}")
            alines.append(f"- Submission types: {', '.join(a.get('submission_types') or [])}")
            alines.append(f"- URL: {a.get('html_url', '')}")
            rubric = a.get("rubric") or []
            if rubric:
                alines.append("- Rubric:")
                for cr in rubric:
                    alines.append(f"  - {cr.get('description', '')} ({cr.get('points', 0)} pts)")
            body = strip_html(a.get("description", ""))
            alines.append("\n" + (body if body else "_(no description)_") + "\n")
            summary["assignments"] += 1
            all_assignments.append({
                "course": label, "name": a.get("name", "Untitled"),
                "due": a.get("due_at"), "points": a.get("points_possible") or 0,
                "status": status, "url": a.get("html_url", ""),
            })
        write(cdir / "assignments.md", "\n".join(alines))

        # announcements.md
        anns = fetch_all(f"/courses/{cid}/discussion_topics", {"only_announcements": "true"}) or []
        nlines = [f"# Announcements — {label}\n"]
        for an in anns:
            nlines.append(f"## {an.get('title', 'Announcement')}")
            nlines.append(f"- Posted: {an.get('posted_at') or an.get('created_at') or ''}")
            nlines.append(f"- URL: {an.get('html_url', '')}\n")
            nlines.append(strip_html(an.get("message", "")) + "\n")
            summary["announcements"] += 1
        write(cdir / "announcements.md", "\n".join(nlines))

        # modules.md
        mods = fetch_all(f"/courses/{cid}/modules", {"include[]": "items"}) or []
        mlines = [f"# Modules — {label}\n"]
        for m in mods:
            if m.get("state") == "unpublished":
                continue
            mlines.append(f"## {m.get('name', 'Module')}")
            for it in (m.get("items") or []):
                if it.get("type") == "SubHeader":
                    mlines.append(f"### {it.get('title', '')}")
                    continue
                mlines.append(f"- [{it.get('type', '')}] {it.get('title', '')} "
                              f"{it.get('html_url', '')}".rstrip())
            mlines.append("")
            summary["modules"] += 1
        write(cdir / "modules.md", "\n".join(mlines))

        # pages.md
        pages = fetch_all(f"/courses/{cid}/pages") or []
        plines = [f"# Pages — {label}\n"]
        for pg in pages:
            slug = pg.get("url")
            full_pg = fetch(f"/courses/{cid}/pages/{slug}") if slug else None
            plines.append(f"## {pg.get('title', slug or 'Page')}")
            plines.append(strip_html((full_pg or {}).get("body", "")) + "\n")
            summary["pages"] += 1
        write(cdir / "pages.md", "\n".join(plines))

        # files/
        files = fetch_all(f"/courses/{cid}/files") or []
        for f in files:
            name = safe_file(f.get("display_name") or f.get("filename") or str(f.get("id")))
            res = download_file(f.get("url", ""), cdir / "files" / name)
            if res.startswith("ok") or res == "skip-exists":
                summary["files"] += 1

        line = (f"- **{label}** (id {cid}): {summary['assignments']} assignments, "
                f"{summary['announcements']} announcements, {summary['modules']} modules, "
                f"{summary['pages']} pages, {summary['files']} files → `{cdir.name}/`")
        index_lines.append(line)
        print(line.replace("**", ""))
    except Exception as e:
        index_lines.append(f"- **{label}** (id {cid}): EXPORT ERROR — {e}")
        print(f"{label}: ERROR {e}")

write(OUT / "INDEX.md", "\n".join(index_lines) + "\n")


# ---- aggregate ASSIGNMENTS.md digest (all courses, upcoming first) --------

def due_key(a):
    if not a["due"]:
        return (1, "")  # no-due after dated
    return (0, a["due"])


def parse_due(s):
    if not s:
        return None
    try:
        return datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


upcoming, past, undated = [], [], []
for a in all_assignments:
    d = parse_due(a["due"])
    if d is None:
        undated.append(a)
    elif d >= now:
        upcoming.append(a)
    else:
        past.append(a)
upcoming.sort(key=lambda a: a["due"])
past.sort(key=lambda a: a["due"], reverse=True)

dlines = [f"# All Assignments — across {len(courses)} courses",
          f"Generated: {now.astimezone().strftime('%Y-%m-%d %H:%M %Z')}",
          "Full description + rubric for each is in that course's `assignments.md`.\n"]


def fmt(a):
    due = a["due"][:10] if a["due"] else "no due date"
    return f"- [{a['course']}] {a['name']} — due {due} · {a['points']}pts · {a['status']} · {a['url']}"


dlines.append(f"## ⏳ Upcoming / not yet due ({len(upcoming)})")
dlines += [fmt(a) for a in upcoming] or ["_(none)_"]
dlines.append(f"\n## 📌 No due date ({len(undated)})")
dlines += [fmt(a) for a in undated] or ["_(none)_"]
dlines.append(f"\n## ✅ Past ({len(past)})")
dlines += [fmt(a) for a in past[:60]]
write(OUT / "ASSIGNMENTS.md", "\n".join(dlines) + "\n")

print(f"\nExport complete → {OUT} ({len(all_assignments)} assignments digested)")
