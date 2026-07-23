import json, sys, os, re, shutil, subprocess, datetime, urllib.request, urllib.parse
from pathlib import Path
from html.parser import HTMLParser

BASE_DIR = Path(os.environ.get('CANVAS_CHECK_HOME', Path.home() / 'canvas-check'))
MANIFEST = str(BASE_DIR / 'manifest.json')
COMPLETED_DIR = str(BASE_DIR / 'completed')
CLAUDE = shutil.which('claude')
if not CLAUDE:
    sys.exit("ERROR: 'claude' CLI not found on PATH — install it or add it to PATH")

# Who the drafts are for; appears in the LLM prompt.
STUDENT_NAME = os.environ.get('STUDENT_NAME', 'the student')


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

# Canvas API (cookie written by auto_login.py) — used to scrape full assignment content.
COOKIE = (BASE_DIR / 'canvas_session.txt').read_text().strip()
API = os.environ.get('CANVAS_BASE_URL', 'https://canvas.instructure.com').rstrip('/') + '/api/v1'
HEADERS = {'Cookie': f'canvas_session={COOKIE}', 'Accept': 'application/json'}

# Comma-separated keywords marking assignments that can't be drafted as text
# (in-person events, attendance, physical hand-ins, ...). Example:
#   SKIP_KEYWORDS="attend,attendance,in-person,in person,come to class,sign in,ticket,museum"
SKIP_KEYWORDS = [k.strip().lower() for k in os.environ.get('SKIP_KEYWORDS', '').split(',') if k.strip()]
# Submission types with no draftable text/upload answer (quizzes launched in an
# external tool, Scantron/paper, ungraded, etc.).
UNDRAFTABLE_TYPES = {'external_tool', 'online_quiz', 'on_paper', 'none', 'not_graded', ''}


def fetch(path, params=None):
    url = path if path.startswith('http') else API + path
    if params:
        url += '?' + urllib.parse.urlencode(params, doseq=True)
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=HEADERS), timeout=25) as r:
            return json.loads(r.read())
    except Exception:
        return None


class _Stripper(HTMLParser):
    def __init__(self):
        super().__init__()
        self.t = []

    def handle_data(self, d):
        self.t.append(d)

    def handle_starttag(self, tag, attrs):
        if tag in ('p', 'br', 'div', 'li', 'tr', 'h1', 'h2', 'h3'):
            self.t.append('\n')


def strip_html(h):
    if not h:
        return ''
    p = _Stripper()
    p.feed(h)
    return re.sub(r'\n{3,}', '\n\n', ''.join(p.t)).strip()


def load_manifest():
    try:
        return json.load(open(MANIFEST))
    except Exception:
        return {}


def save_manifest(m):
    json.dump(m, open(MANIFEST, 'w'), indent=2)


def draftable(a, detail):
    """Whether it's worth drafting a text answer. Skips submitted, zero-point, long-overdue,
    keyword-excluded (in-person etc.), and submission types we can't answer as text."""
    if a.get('submitted'):
        return False
    if a.get('pts', 0) == 0:
        return False
    if a.get('delta_hours', 0) < -14 * 24:
        return False
    types = set((detail or {}).get('submission_types') or [])
    if types and types.issubset(UNDRAFTABLE_TYPES):
        return False
    desc_lower = (a.get('description', '') + ' ' + a.get('name', '')).lower()
    if any(kw in desc_lower for kw in SKIP_KEYWORDS):
        return False
    return True


def scrape_content(a, detail):
    """Assemble the richest available assignment context from Canvas: the full (untruncated)
    description, the discussion prompt, the rubric, and the text of any course Pages linked
    from the description. Falls back to the briefing's short description if the API is down."""
    if not detail:
        return a.get('description', '')
    cid = a.get('course_id')
    html = detail.get('description') or ''
    desc = strip_html(html)
    parts = []
    if desc:
        parts.append('ASSIGNMENT DETAILS:\n' + desc)

    dt = detail.get('discussion_topic') or {}
    msg = strip_html(dt.get('message') or '')
    if msg and msg not in desc:
        parts.append('DISCUSSION PROMPT:\n' + msg)

    rubric = detail.get('rubric') or []
    if rubric:
        rlines = []
        for c in rubric:
            ld = strip_html(c.get('long_description', '') or '')
            rlines.append(f"- {c.get('description', '')} ({c.get('points', 0)} pts)" + (f": {ld}" if ld else ''))
        parts.append('RUBRIC (address every criterion):\n' + '\n'.join(rlines))

    # Follow links to course Pages (assigned readings) embedded in the description.
    if cid and html:
        seen = set()
        for slug in re.findall(r'/courses/%s/pages/([^"#?\s<]+)' % cid, html):
            slug = urllib.parse.unquote(slug)
            if slug in seen:
                continue
            seen.add(slug)
            pg = fetch(f'/courses/{cid}/pages/{slug}') or {}
            body = strip_html(pg.get('body') or '')
            if body:
                parts.append(f"LINKED READING — {pg.get('title', slug)}:\n{body[:3500]}")
            if len(seen) >= 4:
                break

    content = '\n\n'.join(parts).strip()
    return content[:9000] if content else a.get('description', '')


def build_prompt(a, content):
    name = a['name']
    course_name = a.get('course_name', a.get('course', ''))
    course_code = a.get('course_code', '')
    pts = a.get('pts', 0)
    due = a.get('label', '')
    course_section = f'Course: {course_name} ({course_code})' if course_code else f'Course: {course_name}'
    body = content.strip() or '(No assignment content available.)'

    return f"""Write a draft response for this university assignment. The draft is for {STUDENT_NAME}, who will review and rewrite it in their own words before submitting.

{course_section}
Assignment: {name}
Points: {pts} | {due}

{body}

Write the draft exactly as it should appear when submitted:
- Clean prose, no headers unless the assignment format requires them
- No preamble, no meta-commentary, no "here is my draft"
- Match the assignment type: journal/reflection = personal first-person voice; reading response = analytical but grounded; problem set explanation = precise and clear
- Appropriate length for the point value and description (1pt journal ~150 words, 5pt essay ~400 words, 10pt response ~600 words)
- Engage directly with the specific readings, rubric criteria, and prompt details above
- Write the best draft you can from the content provided. Do NOT ask the student for information that is already above, and do NOT refuse — if a specific detail genuinely isn't provided, make a reasonable, clearly-grounded choice and write the draft anyway."""


def complete(a, content):
    result = subprocess.run(
        [CLAUDE, '-p', build_prompt(a, content)],
        capture_output=True, text=True, timeout=180
    )
    out = result.stdout.strip()
    # Never cache an unauthenticated/broken CLI response (it prints its error to stdout).
    if result.returncode != 0 or not out:
        return ''
    if any(x in out[:200].lower() for x in
           ('api error', 'authentication', 'not logged in', '401', 'failed to authenticate')):
        return ''
    return out


def main():
    assignments = json.loads(sys.argv[1]) if len(sys.argv) > 1 else []
    manifest = load_manifest()
    os.makedirs(COMPLETED_DIR, exist_ok=True)

    output = []

    for a in assignments:
        aid = str(a['id'])
        if int(aid) in EXCLUDE:
            continue
        name = a['name']
        url = a.get('url', '')

        if a.get('submitted'):
            if aid in manifest:
                manifest[aid]['submitted'] = True
                save_manifest(manifest)
            continue

        if aid in manifest and manifest[aid].get('file'):
            fp = manifest[aid]['file']
            if os.path.exists(fp) and not manifest[aid].get('submitted'):
                output.append({'id': aid, 'name': name, 'file': fp, 'url': url})
            continue

        detail = fetch(f"/courses/{a.get('course_id')}/assignments/{a['id']}") if a.get('course_id') else None
        if not draftable(a, detail):
            continue

        content = scrape_content(a, detail)
        answer = complete(a, content)
        if not answer:
            continue

        safe_name = name[:35].replace('/', '_').replace(' ', '_').replace(':', '').replace('?', '')
        filepath = f'{COMPLETED_DIR}/{aid}_{safe_name}.md'
        with open(filepath, 'w') as f:
            f.write(answer)

        manifest[aid] = {
            'name': name,
            'file': filepath,
            'completed_at': datetime.datetime.now().isoformat(),
            'submitted': False
        }
        save_manifest(manifest)
        output.append({'id': aid, 'name': name, 'file': filepath, 'url': url})

    print(json.dumps(output))

main()
