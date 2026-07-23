# Runbook — canvas-briefing-bot

## Where it runs

A single always-on machine (a spare Mac or Linux box works). Everything the bot
needs — scripts, venv, credentials, session cookie, Chrome profile, drafts — lives
under one directory, `$CANVAS_CHECK_HOME` (default `~/canvas-check`).

## Trigger

One scheduled invocation of `canvas_check.sh` per day (morning works best — the
briefing is a "what needs doing today" card).

- **macOS (launchd)** — a LaunchAgent with `ProgramArguments` pointing at
  `canvas_check.sh`, `StartCalendarInterval` for the daily time, and an
  `EnvironmentVariables` block for `CANVAS_CHECK_HOME` / `CANVAS_BASE_URL`
  (launchd does not source shell rc files).
- **Linux (cron)** — `0 8 * * * CANVAS_CHECK_HOME=... CANVAS_BASE_URL=... /path/to/canvas_check.sh`

The script is self-contained per run: it re-authenticates from scratch every time,
so there is no long-lived daemon and no state to warm up.

## Credentials it needs

All are files under `$CANVAS_CHECK_HOME`, `chmod 600`, never in the repo:

| File | Contents | Used by |
|---|---|---|
| `.telegram_creds` | `TELEGRAM_BOT_TOKEN=`, `TELEGRAM_CHAT_ID=` | `canvas_check.sh` |
| `.canvas_creds` | `CANVAS_USER=`, `CANVAS_PASS=` (institution SSO login) | `auto_login.py` |
| `.duo_totp` | `{"secret": "<base32>", "counter": N}` — HOTP seed from `duo_enroll.py` | `duo_hotp.py` |
| `.anthropic_key` | `export ANTHROPIC_API_KEY=...` | claude CLI (summaries + drafts) |

### Rotation

- **Telegram token** — revoke/regenerate via @BotFather, update `.telegram_creds`. Takes effect next run.
- **SSO password** — change it at the institution, update `.canvas_creds`. The Duo
  trust cookie in `chrome-profile/` survives a password change.
- **Duo HOTP seed** — in the institution's Duo device portal, add a new device
  ("Duo Mobile", "I have a tablet" avoids needing a phone number), copy the
  activation QR payload/link, run `python duo_enroll.py "<payload>"`. This writes a
  fresh `.duo_totp` with counter 0. Remove the old device afterwards.
- **Anthropic key** — rotate in the Anthropic console, update `.anthropic_key`.

## Failure signature

Failures self-report to the same Telegram chat, prefixed `⚠️ Canvas Check`:

| Alert | Meaning | Exit path |
|---|---|---|
| `SSO creds missing` | `.canvas_creds` absent/incomplete | `auto_login.py` rc=4 |
| `Duo passcode step failed` | HOTP counter desynced past the look-ahead window, or Duo changed its UI | rc=5 |
| `Auto-login failed (rc=N)` | Password rejected or SSO flow changed | rc=2 |
| `401 right after auto-login` | Cookie written but rejected — instance/URL mismatch | — |
| `briefing failed to build` | Canvas API errors; details in `canvas_check.log` | — |

**Silence is also a signature**: no briefing by the scheduled time means the
scheduler didn't fire or the machine is offline. Check
`launchctl list` / `crontab -l` and `$CANVAS_CHECK_HOME/canvas_check.log` first.

## Recovery steps

1. **Read the log**: `tail -50 $CANVAS_CHECK_HOME/canvas_check.log`.
2. **Run by hand** with output visible: `CANVAS_CHECK_HOME=... ./canvas_check.sh`.
3. **Duo counter desync** (repeated rc=5): re-enroll a fresh token with
   `duo_enroll.py` — this resets the counter to 0. Never hand-edit the counter
   upward past the server.
4. **Duo UI changed** (selectors stale): run `duo_probe.py` (dev-only; consumes no
   passcodes) to inventory the new screen flow, then update the selector lists at
   the top of `auto_login.py`.
5. **Stale Chrome lock** ("profile already in use"): the script clears this itself,
   but a hard-wedged profile can be deleted — `rm -rf $CANVAS_CHECK_HOME/chrome-profile`.
   The next login will need a fresh Duo passcode (handled automatically) and will
   re-establish browser trust.
6. **First run after any reset**: the briefing seeds `state.json` silently — that's
   expected, not a failure.
