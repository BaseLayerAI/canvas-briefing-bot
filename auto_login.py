#!/usr/bin/env python3
"""Automated Canvas login via headless Playwright through institution SSO.

Institution auth cookies are session-scoped and there is no persistent SSO cookie,
so we log in from scratch each run. Duo 2FA is remembered in the persistent Chrome
profile (browsertrust cookie), so no phone tap is needed — only username/password
are filled on the Shibboleth form. On success, writes a fresh canvas_session.

Credentials come from $CANVAS_CHECK_HOME/.canvas_creds (chmod 600):
    CANVAS_USER=your_sso_username
    CANVAS_PASS=your_password

Exit 0 = logged in, cookie written. 2 = login failed. 4 = missing/bad creds.
5 = Duo passcode step failed / token not enrolled.
"""
import os
import sys
import time
import urllib.parse
from pathlib import Path
from playwright.sync_api import sync_playwright

BASE = Path(os.environ.get("CANVAS_CHECK_HOME", Path.home() / "canvas-check"))
sys.path.insert(0, str(BASE))
try:
    import duo_hotp
except Exception:
    duo_hotp = None

PROFILE = BASE / "chrome-profile"
CREDS = BASE / ".canvas_creds"
COOKIE_FILE = BASE / "canvas_session.txt"
CANVAS_BASE_URL = os.environ.get("CANVAS_BASE_URL", "https://canvas.instructure.com").rstrip("/")
CANVAS_HOST = urllib.parse.urlparse(CANVAS_BASE_URL).netloc.lower()
DASH = CANVAS_BASE_URL + "/"
DEBUG = bool(os.environ.get("DUO_DEBUG"))

USER_SELECTORS = ["#username", "input[name='j_username']", "input[name='username']", "input[type='text']"]
PASS_SELECTORS = ["#password", "input[name='j_password']", "input[name='password']", "input[type='password']"]
SUBMIT_SELECTORS = ["button[name='_eventId_proceed']", "button[type='submit']", "input[type='submit']", "#submit_button"]
DUO_TRUST = ["#trust-browser-button", "button:has-text('Yes, trust browser')",
             "button:has-text('Trust browser')", "#dampen-choice"]
# "Is this your device?" interstitial on untrusted logins
DUO_YES_DEVICE = ["button:has-text('Yes, this is my device')", "button:has-text('Yes, trust')",
                  "[data-testid='trust-this-browser-button']"]
# Duo Universal Prompt selectors (confirmed from live DOM dumps)
DUO_OTHER = ['[data-testid="other-options"]', "button:has-text('Other options')", "text=Other options"]
DUO_PASSCODE_OPTION = ['[data-testid="passcode"]', "button:has-text('Duo Mobile passcode')",
                       "button:has-text('Enter a passcode')", "button:has-text('passcode')",
                       "a:has-text('passcode')"]
DUO_PASSCODE_INPUT = ["#passcode-input", "input[name='passcode-input']",
                      "input[inputmode='numeric']", "input[autocomplete='one-time-code']"]
DUO_PASSCODE_SUBMIT = ['[data-testid="verify-button"]', "button:has-text('Verify')", "button[type='submit']"]


def load_creds():
    if not CREDS.exists():
        print(f"NO_CREDS missing {CREDS}", file=sys.stderr)
        return None, None
    u = pw = None
    for line in CREDS.read_text().splitlines():
        line = line.strip()
        if line.startswith("CANVAS_USER="):
            u = line.split("=", 1)[1].strip()
        elif line.startswith("CANVAS_PASS="):
            pw = line.split("=", 1)[1].strip()
    return u, pw


def try_fill(page, selectors, value):
    for sel in selectors:
        try:
            el = page.query_selector(sel)
            if el and el.is_visible():
                el.fill(value)
                return True
        except Exception:
            continue
    return False


def try_click(page, selectors):
    for sel in selectors:
        try:
            el = page.query_selector(sel)
            if el and el.is_visible():
                el.click()
                return True
        except Exception:
            continue
    return False


def dump_debug(page, tag):
    if not DEBUG:
        return
    try:
        (BASE / f"duo_debug_{tag}.html").write_text(page.content())
        print(f"DEBUG saved duo_debug_{tag}.html url={page.url}", file=sys.stderr)
    except Exception:
        pass


def find_visible(page, selectors):
    for sel in selectors:
        try:
            el = page.query_selector(sel)
            if el and el.is_visible():
                return el
        except Exception:
            continue
    return None


def duo_navigate(page):
    """When the passcode field isn't showing, take ONE step toward it (dismiss the
    'Is this your device?' interstitial, or Other options → Duo Mobile passcode).
    Returns True if it clicked something."""
    if try_click(page, DUO_YES_DEVICE):
        time.sleep(1.5)
        return True
    if try_click(page, DUO_OTHER):
        time.sleep(1.5)
        dump_debug(page, "options")
        try_click(page, DUO_PASSCODE_OPTION)
        time.sleep(1.5)
        return True
    return False


def duo_submit(page, code):
    """Type and submit a passcode. Returns the outcome kind:
      "used"      — server already consumed this counter (skip to next; harmless)
      "incorrect" — rejected (wrong / out of look-ahead window)
      "none"      — no error shown (accepted → login proceeds)
      "exc"       — interaction raised (treat as not-consumed)
    Does NOT touch the stored counter — the caller commits only on confirmed use."""
    inp = find_visible(page, DUO_PASSCODE_INPUT)
    if not inp:
        return None
    try:
        inp.click()
        inp.fill("")
        inp.type(code, delay=80)  # real keystrokes enable the disabled Verify button
        time.sleep(0.6)
        if not try_click(page, DUO_PASSCODE_SUBMIT):
            inp.press("Enter")
        try:
            page.wait_for_load_state("networkidle", timeout=20000)
        except Exception:
            pass
        time.sleep(2.5)
        err = find_visible(page, ["[data-testid='passcode-error-message-text']"])
        msg = (err.inner_text().strip() if err else "")
        if "already been used" in msg.lower():
            kind = "used"
        elif msg:
            kind = "incorrect"
        else:
            kind = "none"
        print(f"DUO_SUBMIT kind={kind} err={msg!r}", file=sys.stderr)
        try_click(page, DUO_TRUST)
        return kind
    except Exception as e:
        print(f"DUO_SUBMIT_EXC {e}", file=sys.stderr)
        return "exc"


def on_dashboard(page, ctx):
    url = page.url.lower()
    if CANVAS_HOST not in url:
        return False
    if any(k in url for k in ("login", "idp", "shibboleth", "/sso", "signin")):
        return False
    return any(c["name"] == "canvas_session" and c.get("value")
               for c in ctx.cookies(CANVAS_BASE_URL))


def main():
    user, pw = load_creds()
    if not user or not pw:
        print("NO_CREDS user/pass not set", file=sys.stderr)
        return 4

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE),
            channel="chrome",
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(DASH, wait_until="domcontentloaded", timeout=45000)

            deadline = time.monotonic() + 120
            filled = False
            enrolled = bool(duo_hotp and duo_hotp.is_enrolled())
            duo_base = duo_hotp.read_counter() if enrolled else 0
            duo_off = 0          # next counter offset to try
            duo_committed = 0    # offsets the server confirmed-consumed ("used") — safe to persist
            duo_pending = None   # offset of last "none" submit (candidate acceptance)
            duo_submits = 0
            duo_steps = 0

            def persist_counter(success):
                # Only advance the stored counter for codes the server actually consumed.
                if not enrolled:
                    return
                if success and duo_pending is not None:
                    duo_hotp.set_counter(duo_base + duo_pending + 1)
                else:
                    duo_hotp.set_counter(duo_base + duo_committed)

            while time.monotonic() < deadline:
                try:
                    if on_dashboard(page, ctx):
                        cookies = ctx.cookies(CANVAS_BASE_URL)
                        sess = next((c for c in cookies if c["name"] == "canvas_session" and c.get("value")), None)
                        COOKIE_FILE.write_text(sess["value"])
                        persist_counter(True)
                        print("OK logged in, canvas_session written")
                        return 0

                    # let any in-flight navigation settle before touching the DOM
                    try:
                        page.wait_for_load_state("domcontentloaded", timeout=8000)
                    except Exception:
                        pass

                    # Shibboleth login form
                    has_pw = page.query_selector("input[type='password']")
                    if not filled and has_pw:
                        try_fill(page, USER_SELECTORS, user)
                        if try_fill(page, PASS_SELECTORS, pw):
                            try_click(page, SUBMIT_SELECTORS)
                            filled = True
                            try:
                                page.wait_for_load_state("networkidle", timeout=20000)
                            except Exception:
                                pass
                            continue

                    # Duo Universal Prompt
                    if "duosecurity.com" in page.url.lower():
                        duo_steps += 1
                        if duo_steps > 14:
                            print("DUO_FAIL too many steps", file=sys.stderr)
                            persist_counter(False)
                            return 5
                        inp = find_visible(page, DUO_PASSCODE_INPUT)
                        if not inp:
                            if not duo_navigate(page):
                                try_click(page, DUO_TRUST)  # interstitial/loading
                            time.sleep(1.5)
                            continue
                        if duo_submits >= 4:
                            print("DUO_FAIL passcode not accepted (cap)", file=sys.stderr)
                            persist_counter(False)
                            return 5
                        code = duo_hotp.peek(duo_base + duo_off)
                        kind = duo_submit(page, code)
                        duo_submits += 1
                        if kind == "used":
                            duo_off += 1
                            duo_committed = duo_off  # this counter is genuinely gone
                            duo_pending = None
                        elif kind == "none":
                            duo_pending = duo_off    # likely accepted → confirm via dashboard
                            duo_off += 1
                        elif kind == "incorrect":
                            print("DUO_FAIL incorrect passcode (out of window)", file=sys.stderr)
                            persist_counter(False)  # never commit an unaccepted code
                            return 5
                        else:  # "exc"/None — interaction failed, code not consumed; retry same offset
                            duo_pending = None
                        time.sleep(2)
                        continue
                    # Trust-browser auto-skip (remember-me still valid) — click if shown
                    try_click(page, DUO_TRUST)
                except Exception:
                    pass  # navigation mid-iteration; retry next loop
                time.sleep(2)

            persist_counter(False)
            if "duosecurity.com" in page.url.lower() and enrolled:
                print(f"DUO_FAIL stuck on Duo; last url={page.url}", file=sys.stderr)
                return 5
            print(f"AUTH_FAIL did not reach dashboard; last url={page.url}", file=sys.stderr)
            return 2
        finally:
            ctx.close()


if __name__ == "__main__":
    sys.exit(main())
