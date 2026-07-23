#!/usr/bin/env python3
"""Duo HOTP passcodes from the enrolled software token, with peek/commit semantics.

$CANVAS_CHECK_HOME/.duo_totp = {"secret": "<base32>", "counter": N}.

Counter drift prevention: callers PEEK a code at a given counter (no state change),
submit it, and only COMMIT (persist) the counter once the server has actually
consumed it. A code burned on a login that fails for unrelated reasons never
advances the stored counter, so local and Duo stay in sync.
"""
import json
import os
from pathlib import Path

import pyotp

TOTP_FILE = Path(os.environ.get("CANVAS_CHECK_HOME", Path.home() / "canvas-check")) / ".duo_totp"


def is_enrolled():
    return TOTP_FILE.exists()


def _load():
    return json.loads(TOTP_FILE.read_text())


def read_counter():
    return int(_load().get("counter", 0))


def set_counter(n):
    d = _load()
    d["counter"] = int(n)
    TOTP_FILE.write_text(json.dumps(d))


def peek(counter):
    """HOTP code at an absolute counter — does NOT change stored state."""
    return pyotp.HOTP(_load()["secret"]).at(int(counter))


def next_code():
    """Legacy: code at the stored counter, then increment+persist. Prefer peek/commit."""
    c = read_counter()
    code = peek(c)
    set_counter(c + 1)
    return code


if __name__ == "__main__":
    print(peek(read_counter()))
