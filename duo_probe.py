#!/usr/bin/env python3
"""DEV-ONLY: map the Duo Universal Prompt screen flow WITHOUT submitting any passcode
(0 codes consumed). Not part of the scheduled run — use it when Duo changes its UI
and auto_login.py's selectors need re-locking.

Logs in with username/password, then on Duo walks: dump → click Yes-device
→ dump → click Other options → dump → click passcode option → dump. Prints a UI
inventory (headings, visible buttons, inputs) at each step so we can lock selectors.
"""
import os
import sys
import time
from pathlib import Path
from playwright.sync_api import sync_playwright

BASE = Path(os.environ.get("CANVAS_CHECK_HOME", Path.home() / "canvas-check"))
PROFILE = BASE / "chrome-profile"
CREDS = BASE / ".canvas_creds"
CANVAS_BASE_URL = os.environ.get("CANVAS_BASE_URL", "https://canvas.instructure.com").rstrip("/")


def creds():
    u = pw = None
    for line in CREDS.read_text().splitlines():
        if line.startswith("CANVAS_USER="):
            u = line.split("=", 1)[1].strip()
        elif line.startswith("CANVAS_PASS="):
            pw = line.split("=", 1)[1].strip()
    return u, pw


def inventory(page, tag):
    print(f"\n===== STEP {tag} | url={page.url[:70]}")
    try:
        for h in page.query_selector_all("h1, h2, h3"):
            if h.is_visible() and h.inner_text().strip():
                print("  H:", h.inner_text().strip()[:80])
        for b in page.query_selector_all("button, a[role=button]"):
            if b.is_visible():
                t = (b.inner_text() or "").strip().replace("\n", " ")
                tid = b.get_attribute("data-testid") or ""
                dis = b.get_attribute("disabled")
                if t or tid:
                    print(f"  BTN: text={t[:45]!r} testid={tid!r} disabled={dis is not None}")
        for i in page.query_selector_all("input"):
            if i.is_visible():
                print(f"  INPUT: id={i.get_attribute('id')} name={i.get_attribute('name')} "
                      f"type={i.get_attribute('type')} inputmode={i.get_attribute('inputmode')}")
    except Exception as e:
        print("  inventory error:", e)
    (BASE / f"probe_{tag}.html").write_text(page.content())


def click_text(page, texts):
    for t in texts:
        for sel in (f"button:has-text(\"{t}\")", f"a:has-text(\"{t}\")", f"[data-testid=\"{t}\"]"):
            try:
                el = page.query_selector(sel)
                if el and el.is_visible():
                    el.click()
                    print(f"  >> clicked {sel}")
                    return True
            except Exception:
                continue
    return False


with sync_playwright() as p:
    ctx = p.chromium.launch_persistent_context(user_data_dir=str(PROFILE), channel="chrome",
                                               headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    user, pw = creds()
    page.goto(CANVAS_BASE_URL + "/", wait_until="domcontentloaded", timeout=45000)
    # shibboleth
    for _ in range(25):
        try:
            if page.query_selector("input[type='password']"):
                page.fill("input[type='password']", pw)
                for s in ("#username", "input[name='j_username']", "input[name='username']", "input[type='text']"):
                    e = page.query_selector(s)
                    if e and e.is_visible():
                        e.fill(user); break
                for s in ("button[name='_eventId_proceed']", "button[type='submit']", "input[type='submit']"):
                    e = page.query_selector(s)
                    if e and e.is_visible():
                        e.click(); break
                break
        except Exception:
            pass
        time.sleep(1)
    # wait for Duo
    for _ in range(20):
        if "duosecurity.com" in page.url:
            break
        time.sleep(1)
    time.sleep(3)

    inventory(page, "1_initial")
    if click_text(page, ["Yes, this is my device", "Yes, trust"]):
        time.sleep(2); inventory(page, "2_after_yesdevice")
    if click_text(page, ["other-options", "Other options"]):
        time.sleep(2); inventory(page, "3_after_otheroptions")
    if click_text(page, ["Duo Mobile passcode", "Enter a passcode", "passcode", "Passcode"]):
        time.sleep(2); inventory(page, "4_after_passcodeopt")
    ctx.close()
    print("\nPROBE DONE")
