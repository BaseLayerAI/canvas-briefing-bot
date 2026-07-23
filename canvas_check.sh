#!/bin/bash
set -euo pipefail

# All state, credentials, and helper scripts live under one directory.
CANVAS_CHECK_HOME="${CANVAS_CHECK_HOME:-$HOME/canvas-check}"

# launchd does not source shell rc files; load the Anthropic key for the
# AI module summaries (claude CLI) from a 600-perm cred file if present.
[ -f "$CANVAS_CHECK_HOME/.anthropic_key" ] && . "$CANVAS_CHECK_HOME/.anthropic_key" || true

# Telegram credentials come from a 600-perm creds file, never from the repo:
#   $CANVAS_CHECK_HOME/.telegram_creds
#     TELEGRAM_BOT_TOKEN=...
#     TELEGRAM_CHAT_ID=...
[ -f "$CANVAS_CHECK_HOME/.telegram_creds" ] && . "$CANVAS_CHECK_HOME/.telegram_creds" || true
: "${TELEGRAM_BOT_TOKEN:?TELEGRAM_BOT_TOKEN not set — create $CANVAS_CHECK_HOME/.telegram_creds (chmod 600)}"
: "${TELEGRAM_CHAT_ID:?TELEGRAM_CHAT_ID not set — create $CANVAS_CHECK_HOME/.telegram_creds (chmod 600)}"

COOKIE_FILE="$CANVAS_CHECK_HOME/canvas_session.txt"
PYTHON="$CANVAS_CHECK_HOME/.venv/bin/python"
REFRESH_SCRIPT="$CANVAS_CHECK_HOME/auto_login.py"
CANVAS_BASE_URL="${CANVAS_BASE_URL:-https://canvas.instructure.com}"
BASE="${CANVAS_BASE_URL%/}/api/v1"
LOG="$CANVAS_CHECK_HOME/canvas_check.log"

send_text() {
  curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
    -d chat_id="$TELEGRAM_CHAT_ID" \
    -d parse_mode="HTML" \
    --data-urlencode "text=$1" > /dev/null
}

send_doc() {
  local filepath="$1"
  local caption="$2"
  curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendDocument" \
    -F chat_id="$TELEGRAM_CHAT_ID" \
    -F caption="$caption" \
    -F document=@"$filepath" > /dev/null
}

send_text_chunked() {
  # Telegram caps messages at 4096 chars; split on newline boundaries under 3500.
  local text="$1" chunk="" line
  while IFS= read -r line; do
    if [ $(( ${#chunk} + ${#line} + 1 )) -gt 3500 ]; then
      [ -n "$chunk" ] && send_text "$chunk"
      chunk="$line"
    else
      chunk="${chunk:+$chunk$'\n'}$line"
    fi
  done <<< "$text"
  [ -n "$chunk" ] && send_text "$chunk"
}

log() { echo "$(date '+%Y-%m-%d %H:%M'): $1" >> "$LOG"; }

# Clear any stale single-instance lock / orphaned automation Chrome on our profile
# so scheduled runs don't fail with "profile already in use". Only targets the
# canvas-check profile, never the user's normal Chrome.
pkill -f "user-data-dir=$CANVAS_CHECK_HOME/chrome-profile" 2>/dev/null || true
sleep 2
rm -f "$CANVAS_CHECK_HOME"/chrome-profile/Singleton* 2>/dev/null || true

# Automated SSO login (Playwright, headless; Duo 2FA remembered) → fresh canvas_session.
# Replaces the old manual-cookie-copy flow that died overnight on SSO idle timeout.
REFRESH_OUT=$("$PYTHON" "$REFRESH_SCRIPT" 2>&1) && REFRESH_RC=0 || REFRESH_RC=$?
if [ "$REFRESH_RC" = "4" ]; then
  send_text "⚠️ <b>Canvas Check</b> — SSO creds missing. Fill \$CANVAS_CHECK_HOME/.canvas_creds (CANVAS_USER / CANVAS_PASS)."
  log "ERROR: missing creds (rc=4): $REFRESH_OUT"
  exit 1
fi
if [ "$REFRESH_RC" = "5" ]; then
  send_text "⚠️ <b>Canvas Check</b> — Duo passcode step failed (token desynced or selectors changed). Re-enroll: add a Duo device, then run duo_enroll.py with the activation code."
  log "ERROR: duo passcode failed (rc=5): $REFRESH_OUT"
  exit 1
fi
if [ "$REFRESH_RC" != "0" ]; then
  send_text "⚠️ <b>Canvas Check</b> — Auto-login failed (rc=$REFRESH_RC). Check creds / SSO flow. Last: $(echo "$REFRESH_OUT" | tail -1)"
  log "ERROR: auto-login failed (rc=$REFRESH_RC): $REFRESH_OUT"
  exit 1
fi

COOKIE=$(cat "$COOKIE_FILE")

# Auth check (belt-and-suspenders; should pass right after auto-login)
STATUS=$(curl -s -o /dev/null -w "%{http_code}" -b "canvas_session=$COOKIE" "$BASE/users/self")
if [ "$STATUS" = "401" ]; then
  send_text "⚠️ <b>Canvas Check</b> — 401 right after auto-login. Re-run auto_login.py on the host and check the Chrome profile."
  log "ERROR: 401 after auto-login"
  exit 1
fi

log "Auth OK. Building briefing..."

# Build daily briefing: new announcements + new content + assignments due
BRIEF=$(cd /tmp && "$PYTHON" "$CANVAS_CHECK_HOME/canvas_briefing.py" 2>>"$LOG")
if [ -z "$BRIEF" ]; then
  send_text "⚠️ <b>Canvas Check</b> — briefing failed to build."
  log "ERROR: briefing produced no output"
  exit 1
fi

MESSAGE=$(echo "$BRIEF" | /usr/bin/jq -r '.message')
ASSIGNMENTS=$(echo "$BRIEF" | /usr/bin/jq -c '.assignments')

send_text_chunked "$MESSAGE"
log "Sent briefing"

# Draft completable assignments and attach as .md for review
if [ -n "$ASSIGNMENTS" ] && [ "$ASSIGNMENTS" != "[]" ] && [ "$ASSIGNMENTS" != "null" ]; then
  FILES=$(cd /tmp && python3 "$CANVAS_CHECK_HOME/canvas_complete.py" "$ASSIGNMENTS" 2>/dev/null)
  if [ -n "$FILES" ] && [ "$FILES" != "[]" ]; then
    echo "$FILES" | /usr/bin/jq -c '.[]' | while read -r item; do
      FILE=$(echo "$item" | /usr/bin/jq -r '.file')
      NAME=$(echo "$item" | /usr/bin/jq -r '.name' | cut -c1-50)
      if [ -f "$FILE" ]; then
        PDF="${FILE%.md}.pdf"
        if python3 "$CANVAS_CHECK_HOME/md_to_pdf.py" "$FILE" "$PDF" >>"$LOG" 2>&1 && [ -s "$PDF" ]; then
          send_doc "$PDF" "📝 Draft to review (rewrite before submitting): $NAME"
          log "Attached (pdf): $NAME"
        else
          send_doc "$FILE" "📝 Draft to review (rewrite before submitting): $NAME"
          log "WARN: pdf convert failed, sent md: $NAME"
        fi
      fi
    done
  fi
fi
log "Briefing complete"

# Refresh the local coursework export (non-fatal; auth already fresh from auto_login).
if "$PYTHON" "$CANVAS_CHECK_HOME/canvas_export.py" >>"$LOG" 2>&1; then
  log "Coursework export refreshed"
else
  log "WARN: coursework export failed (non-fatal)"
fi
