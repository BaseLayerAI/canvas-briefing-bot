# canvas-briefing-bot

Unattended Canvas LMS monitor that logs in through institution SSO + Duo 2FA every
morning and delivers a daily coursework briefing to Telegram.

**Status:** runs daily on a schedule (launchd/cron) on a single always-on machine. No server, no third-party services beyond Telegram and the Canvas instance itself.

## How it works

The hard part is auth: many institutions put Canvas behind Shibboleth SSO with Duo
2FA and session-scoped cookies, so there is no long-lived API token to steal-proof
your way around — the bot performs a real login every run.

```
canvas_check.sh (daily, launchd/cron)
  │
  ├─ auto_login.py ── headless Playwright (persistent Chrome profile)
  │     │               Shibboleth login form → Duo Universal Prompt
  │     │               passcodes generated locally by duo_hotp.py
  │     │               (HOTP seed enrolled once via duo_enroll.py)
  │     └─→ canvas_session.txt   fresh Canvas session cookie
  │
  ├─ canvas_briefing.py ── Canvas REST: todo, announcements, modules
  │     │                  state.json diff → only NEW items
  │     │                  claude CLI → one-line module summaries
  │     └─→ {message, assignments} JSON
  │
  ├─ canvas_complete.py ── per draftable assignment:
  │     │                  full description + rubric + linked pages
  │     │                  claude CLI → draft answer (.md)
  │     └─ md_to_pdf.py ─→ PDF via Playwright page.pdf()
  │
  ├─ canvas_export.py ──→ local Markdown mirror of all courses
  │
  └─ Telegram Bot API ──→ briefing message + draft PDFs
```

Duo specifics worth knowing:

- `duo_enroll.py` enrolls a **software HOTP token** once (speaks Duo's device
  activation protocol directly; feed it the "add device" QR payload instead of
  scanning it with a phone). After that, passcodes are generated locally, offline.
- `duo_hotp.py` uses **peek/commit counter semantics** — a code burned on a login
  that fails for unrelated reasons never advances the stored counter, so the local
  token can't drift out of Duo's look-ahead window.
- Browser trust ("remember me") lives in the persistent Chrome profile, so most
  runs never even reach the passcode screen.
- `duo_probe.py` is **dev-only**: it inventories the Duo screen flow without
  consuming any passcodes, for re-locking selectors when Duo changes its UI.

The briefing itself is diff-based: `state.json` tracks seen announcement/module/item
IDs, so each message contains only what's new plus everything currently due. First
run seeds the baseline silently. Assignments matching `SKIP_KEYWORDS` (in-person
events etc.) or un-draftable submission types are never drafted; drafts are
explicitly framed as material for the student to review and rewrite.

## Quickstart

```bash
git clone https://github.com/BaseLayerAI/canvas-briefing-bot
cd canvas-briefing-bot
cp .env.example .env          # then edit

# runtime home for state + creds
export CANVAS_CHECK_HOME="$HOME/canvas-check"
mkdir -p "$CANVAS_CHECK_HOME"
cp *.py canvas_check.sh "$CANVAS_CHECK_HOME/"

# venv with Playwright Chrome
python3 -m venv "$CANVAS_CHECK_HOME/.venv"
"$CANVAS_CHECK_HOME/.venv/bin/pip" install -r requirements.txt
"$CANVAS_CHECK_HOME/.venv/bin/playwright" install chrome

# credentials (all chmod 600, see .env.example for formats)
$EDITOR "$CANVAS_CHECK_HOME/.telegram_creds"   # TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID
$EDITOR "$CANVAS_CHECK_HOME/.canvas_creds"     # CANVAS_USER / CANVAS_PASS
$EDITOR "$CANVAS_CHECK_HOME/.anthropic_key"    # export ANTHROPIC_API_KEY=...
chmod 600 "$CANVAS_CHECK_HOME"/.telegram_creds "$CANVAS_CHECK_HOME"/.canvas_creds "$CANVAS_CHECK_HOME"/.anthropic_key

# enroll the Duo software token once (activation payload from "add device" QR)
"$CANVAS_CHECK_HOME/.venv/bin/python" "$CANVAS_CHECK_HOME/duo_enroll.py" "<code>-<base64host>"

# first run (seeds the baseline, sends a briefing)
CANVAS_BASE_URL=https://canvas.myuniversity.edu "$CANVAS_CHECK_HOME/canvas_check.sh"
```

Then install the daily trigger (launchd on macOS, cron on Linux) — see
[docs/RUNBOOK.md](docs/RUNBOOK.md).

## Environment

| Variable | Default | Purpose |
|---|---|---|
| `CANVAS_CHECK_HOME` | `~/canvas-check` | Root for state, creds, Chrome profile, drafts |
| `CANVAS_BASE_URL` | `https://canvas.instructure.com` | Your Canvas instance; `/api/v1` is appended |
| `STUDENT_NAME` | `the student` | Who drafts are addressed to in the LLM prompt |
| `SKIP_KEYWORDS` | *(empty)* | Comma-separated markers of un-draftable assignments |
| `CANVAS_EXPORT_DIR` | `~/canvas-export` | Where the local coursework mirror is written |
| `RENDER_PYTHON` | venv python | Playwright-equipped interpreter for PDF rendering |
| `DUO_DEBUG` | *(unset)* | Dump Duo page HTML during login for debugging |

Credential **files** (`.telegram_creds`, `.canvas_creds`, `.duo_totp`,
`.anthropic_key`) are documented in [.env.example](.env.example).

## Deployment

Single machine, single scheduled job. The script re-authenticates from scratch on
every run and cleans up its own stale Chrome locks, so recovery from crashes or
reboots is just "wait for the next scheduled run". Operational details, failure
signatures, and credential rotation: [docs/RUNBOOK.md](docs/RUNBOOK.md).

## Security note — read this before deploying

This bot stores your **SSO password and a Duo HOTP seed on disk**. That is a
deliberate trade: it converts your second factor into something a scheduled job can
use, which necessarily weakens it — anyone with the files in `$CANVAS_CHECK_HOME`
can log in as you. Mitigations, not solutions:

- keep every credential file `chmod 600` on a machine only you control,
  ideally dedicated to the job;
- enroll a **separate** Duo device for the bot so it can be revoked independently
  in the Duo portal at any time;
- nothing in this repo ever contains credentials — they live only under
  `$CANVAS_CHECK_HOME`, which is gitignored in all its parts.

If that trade isn't acceptable for your threat model, don't run this.
