#!/usr/bin/env python3
"""Build a daily Canvas briefing, grouped by course and led by an action verdict.

Open it and instantly know whether something needs doing today. Each class is its
own block: assignments due, new modules (with a one-line AI summary), new content
items, and new announcements. Quiet classes are hidden.

Auth: reads canvas_session.txt (written by auto_login.py). State: state.json tracks
seen announcement / module / module-item IDs so each briefing shows only new items.
First run seeds state silently (no whole-semester flood) but still lists due work.

Output (stdout, JSON):
    {"message": "<telegram markdown>", "assignments": [ ... ], "has_content": bool}
"""
import json
import os
import re
import datetime
import shutil
import subprocess
import sys
import urllib.request
import urllib.parse
from pathlib import Path
from html.parser import HTMLParser

BASE_DIR = Path(os.environ.get("CANVAS_CHECK_HOME", Path.home() / "canvas-check"))
COOKIE = (BASE_DIR / "canvas_session.txt").read_text().strip()
STATE_FILE = BASE_DIR / "state.json"
API = os.environ.get("CANVAS_BASE_URL", "https://canvas.instructure.com").rstrip("/") + "/api/v1"
HEADERS = {"Cookie": f"canvas_session={COOKIE}", "Accept": "application/json"}
CLAUDE = shutil.which("claude")
if not CLAUDE:
    print("WARN: 'claude' CLI not found on PATH — module summaries fall back to module names",
          file=sys.stderr)


def load_excludes():
    """Assignment IDs the user opted out of - never shown or drafted. One id per line
    in $CANVAS_CHECK_HOME/.exclude_ids ('#' comments allowed)."""
    f = BASE_DIR / ".exclude_ids"
    ids = set()
    if f.exists():
        for line in f.read_text().splitlines():
            line = line.split("#", 1)[0].strip()
            if line.isdigit():
                ids.add(int(line))
    return ids


EXCLUDE = load_excludes()

now = datetime.datetime.now(datetime.timezone.utc)


def fetch(path, params=None):
    url = f"{API}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params, doseq=True)
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())
    except Exception:
        return None


class _Stripper(HTMLParser):
    def __init__(self):
        super().__init__()
        self.t = []

    def handle_data(self, d):
        self.t.append(d)


def strip_html(h):
    if not h:
        return ""
    p = _Stripper()
    p.feed(h)
    return " ".join(p.t).strip()


def html_safe(s):
    # escape for Telegram HTML parse mode
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").strip()


def link(text, url, limit=60):
    t = html_safe(text)[:limit]
    if url:
        return f'<a href="{html_safe(url)}">{t}</a>'
    return t


def short_label(course_code, name):
    """25F-BIO-101-LEC-1 / 25F-BIO-101-LEC-2  ->  BIO 101. Fallback to a clean code/name."""
    base = (course_code or name or "").split("/")[0].strip()
    m = re.search(r"([A-Z]{2,})-?(\d+[A-Z]*)", base)
    if m:
        return f"{m.group(1)} {m.group(2)}"
    return html_safe(course_code or name or "")


def summarize_module(name, item_titles):
    """One-line AI summary of a new module. Falls back to the module name on failure."""
    if not CLAUDE:
        return html_safe(name)
    items = ", ".join(t for t in item_titles if t)[:600]
    prompt = (
        "In one line (max 15 words), summarize what this course module covers. "
        f"Title: {name}. Items: {items}. Reply with only the summary, no preamble."
    )
    try:
        r = subprocess.run([CLAUDE, "-p", prompt], capture_output=True, text=True, timeout=60)
        line = r.stdout.strip().splitlines()[0] if r.stdout.strip() else ""
        # A broken/unauthenticated CLI prints its error to stdout; never leak that
        # into the briefing -- fall back to the module name instead.
        if r.returncode != 0 or not line or re.search(
                r"API Error|authentication|Not logged in|401|Failed to", line, re.I):
            return html_safe(name)
        return html_safe(line)
    except Exception:
        return html_safe(name)


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return None


def save_state(state):
    STATE_FILE.write_text(json.dumps(state))


# ---- gather ---------------------------------------------------------------

courses = fetch("/courses", {"enrollment_state": "active", "per_page": 50}) or []
courses = [c for c in courses if c.get("id") and not c.get("access_restricted_by_date")]
course_label = {c["id"]: short_label(c.get("course_code"), c.get("name")) for c in courses}
course_ids = list(course_label.keys())

# per-course buckets
buckets = {cid: {"label": course_label[cid], "assignments": [], "new_modules": [],
                 "new_items": [], "announcements": []} for cid in course_ids}

state = load_state()
first_run = state is None
if first_run:
    state = {"announcements": [], "modules": [], "module_items": []}
seen_ann = set(state.get("announcements", []))
seen_mod = set(state.get("modules", []))
seen_item = set(state.get("module_items", []))

# Announcements across all courses (last 21 days window)
n_new_ann = 0
if course_ids:
    ctx = [f"course_{cid}" for cid in course_ids]
    start = (now - datetime.timedelta(days=21)).date().isoformat()
    anns = fetch("/announcements", {"context_codes[]": ctx, "start_date": start,
                                    "active_only": "true", "per_page": 50}) or []
    for a in anns:
        aid = a.get("id")
        if aid is None:
            continue
        if aid not in seen_ann:
            cc = a.get("context_code", "")
            cid = int(cc.split("_", 1)[1]) if cc.startswith("course_") else None
            if cid in buckets:
                buckets[cid]["announcements"].append({
                    "title": html_safe(a.get("title", "Announcement")),
                    "url": a.get("html_url", ""),
                })
                n_new_ann += 1
        seen_ann.add(aid)

# Modules + items per course
for cid in course_ids:
    mods = fetch(f"/courses/{cid}/modules", {"include[]": "items", "per_page": 50}) or []
    for m in mods:
        mid = m.get("id")
        if mid is None or m.get("state") == "unpublished":
            continue
        is_new_mod = mid not in seen_mod
        seen_mod.add(mid)
        item_titles = []
        new_item_objs = []
        for it in (m.get("items") or []):
            iid = it.get("id")
            if iid is None or it.get("type") in ("SubHeader",):
                continue
            title = html_safe(it.get("title", ""))
            item_titles.append(title)
            if iid not in seen_item:
                new_item_objs.append({"title": title, "url": it.get("html_url", "")})
            seen_item.add(iid)
        # On first run, suppress modules/items entirely (baseline seed only).
        if first_run:
            continue
        if is_new_mod:
            summary = summarize_module(html_safe(m.get("name", "Module")), item_titles)
            buckets[cid]["new_modules"].append({"name": html_safe(m.get("name", "Module")),
                                                "summary": summary})
        else:
            buckets[cid]["new_items"].extend(new_item_objs)

# Assignments due (todo)
all_assignments = []
todo = fetch("/users/self/todo", {"per_page": 30}) or []
for item in todo:
    a = item.get("assignment", {})
    aid = a.get("id")
    due_str = a.get("due_at", "")
    if not aid or not due_str or aid in EXCLUDE:
        continue
    due = datetime.datetime.fromisoformat(due_str.replace("Z", "+00:00"))
    delta = (due - now).total_seconds() / 3600
    cid = a.get("course_id") or item.get("course_id")
    if delta < 0:
        label, emoji = f"OVERDUE {abs(int(delta // 24))}d", "🔴"
    elif delta < 24:
        label, emoji = f"due in {int(delta)}h", "🔥"
    elif delta < 72:
        label, emoji = f"due in {int(delta // 24)}d", "⚠️"
    else:
        label, emoji = "due " + due.strftime("%b %d"), "📅"
    rec = {
        "id": aid,
        "name": html_safe(a.get("name", "")),
        "url": a.get("html_url", ""),
        "course": course_label.get(cid, html_safe(item.get("context_name", ""))),
        "course_id": cid,
        "label": label,
        "emoji": emoji,
        "delta_hours": round(delta, 1),
        "pts": int(a.get("points_possible") or 0),
        "description": strip_html(a.get("description", ""))[:800],
    }
    all_assignments.append(rec)
    if cid in buckets:
        buckets[cid]["assignments"].append(rec)
all_assignments.sort(key=lambda x: x["delta_hours"])
for b in buckets.values():
    b["assignments"].sort(key=lambda x: x["delta_hours"])

# ---- persist state --------------------------------------------------------

save_state({
    "last_run": now.isoformat(),
    "announcements": sorted(seen_ann),
    "modules": sorted(seen_mod),
    "module_items": sorted(seen_item),
})

# ---- build message --------------------------------------------------------

DIV = "━" * 15
dt = now.astimezone()
date_str = dt.strftime("%A, %b ") + str(dt.day)
n_due = len(all_assignments)
n_new_content = sum(len(b["new_modules"]) + len(b["new_items"]) for b in buckets.values())
action = (n_due > 0) or (n_new_ann > 0)
header = f"📚 <b>Canvas Briefing</b> · {date_str}"


def course_sort_key(item):
    cid, b = item
    return (0 if b["assignments"] else 1 if b["announcements"] else 2,
            b["assignments"][0]["delta_hours"] if b["assignments"] else 0,
            b["label"])


def plural(n, word):
    return f"{n} {word}" + ("s" if n != 1 else "")


# Per-course blocks (each begins with a hairline divider)
blocks = []
for cid, b in sorted(buckets.items(), key=course_sort_key):
    if not (b["assignments"] or b["new_modules"] or b["new_items"] or b["announcements"]):
        continue
    block = [DIV, f"<b>{b['label'][:38]}</b>"]
    for a in b["assignments"]:
        block.append(f"  {a['emoji']} {link(a['name'], a['url'], 55)} — {a['label']}")
    for m in b["new_modules"]:
        block.append(f"  🆕 {m['name'][:45]} — {m['summary'][:90]}")
    for it in b["new_items"][:8]:
        block.append(f"  ➕ {link(it['title'], it['url'])}")
    for an in b["announcements"][:8]:
        block.append(f"  📢 {link(an['title'], an['url'])}")
    blocks.append("\n".join(block))

emitted = len(blocks)
has_content = bool(emitted)

if emitted == 0 and not first_run:
    # Clean caught-up card
    message = f"{header}\n🟢 <b>All caught up</b> — no action today"
else:
    if first_run:
        status = "<i>baseline set — future briefings show only new items + due work</i>"
    elif action:
        bits = []
        if n_due:
            bits.append(plural(n_due, "due").replace("dues", "due"))
        if n_new_ann:
            bits.append(plural(n_new_ann, "announcement"))
        status = f"🔴 <b>Action needed</b> — {', '.join(bits)}"
    else:
        status = "🟢 <b>No action</b> — content updates only"

    footer_bits = [plural(emitted, "class").replace("classs", "classes")]
    if n_due:
        footer_bits.append("{} due".format(n_due))
    if n_new_ann:
        footer_bits.append(plural(n_new_ann, "announcement"))
    if n_new_content:
        footer_bits.append(f"{n_new_content} new")
    footer = f"{DIV}\n📊 " + " · ".join(footer_bits)

    message = f"{header}\n{status}\n\n" + "\n\n".join(blocks) + f"\n\n{footer}"

print(json.dumps({"message": message, "assignments": all_assignments, "has_content": has_content}))
