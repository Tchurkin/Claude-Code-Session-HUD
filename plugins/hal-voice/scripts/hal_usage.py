#!/usr/bin/env python3
"""
Subscription usage - the real numbers, not an estimate.

How much of your session you've spent is the one thing the HUD couldn't work out locally. Transcripts
record every token, but a plan's limits are weighted per model and live server-side, so a local tally
can tell you what you burned and never what you have left. Claude Code's own ``/usage`` gets the real
figures from ``/api/oauth/usage``; this asks the same endpoint, with the OAuth token Claude Code has
already stored on this machine.

Deliberately hands-off about credentials: it reads the token fresh each time and never refreshes,
rewrites or transmits it anywhere but api.anthropic.com. When the token has expired the fetch simply
fails and the meter goes stale until Claude Code renews it on its own - which it does, in the normal
course of being used.
"""
import json, os, time, urllib.request

import hal_common as hc

CLAUDE_DIR  = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(hc.HOME, ".claude")
CREDENTIALS = os.path.join(CLAUDE_DIR, ".credentials.json")
CACHE       = os.path.join(hc.DATA_DIR, "usage.json")
ENDPOINT    = "https://api.anthropic.com/api/oauth/usage"
POLL_MS     = 60000          # the numbers move slowly; one request a minute is plenty


def _token():
    try:
        with open(CREDENTIALS, encoding="utf-8-sig") as f:
            return (json.load(f).get("claudeAiOauth") or {}).get("accessToken")
    except Exception:
        return None


def _pct(block):
    try:
        v = block.get("utilization")
        return None if v is None else max(0, min(100, int(round(float(v)))))
    except Exception:
        return None


def fetch(timeout=10):
    """Current usage, or None if it can't be had. Never raises."""
    tok = _token()
    if not tok:
        return None
    try:
        req = urllib.request.Request(ENDPOINT, headers={
            "Authorization": "Bearer %s" % tok,
            "Content-Type": "application/json",
            "anthropic-beta": "oauth-2025-04-20",
            "User-Agent": "claude-session-hud",
        })
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read().decode("utf-8"))
    except Exception:
        return None
    if not isinstance(d, dict):
        return None
    five, week = d.get("five_hour") or {}, d.get("seven_day") or {}
    sev = "normal"
    for lim in (d.get("limits") or []):                  # the API's own severity, when it offers one
        if lim.get("kind") == "session" and lim.get("severity"):
            sev = str(lim["severity"])
    return {"ts": int(time.time() * 1000),
            "session_pct": _pct(five), "session_resets": five.get("resets_at"),
            "weekly_pct": _pct(week),  "weekly_resets": week.get("resets_at"),
            "severity": sev}


def read():
    try:
        with open(CACHE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def refresh(force=False):
    """Fetch at most once a minute and publish for the overlays to draw. Returns the current values."""
    cur = read()
    try:
        fresh = (time.time() * 1000 - float(cur.get("ts") or 0)) < POLL_MS
    except Exception:
        fresh = False
    if fresh and not force:
        return cur
    got = fetch()
    if not got:
        return cur                                        # keep showing the last good numbers
    try:
        os.makedirs(hc.DATA_DIR, exist_ok=True)
        tmp = CACHE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(got, f)
        os.replace(tmp, CACHE)
    except Exception:
        pass
    return got


if __name__ == "__main__":
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    u = refresh(force=True)
    print("session %s%%  (resets %s)" % (u.get("session_pct"), u.get("session_resets")))
    print("weekly  %s%%  (resets %s)" % (u.get("weekly_pct"), u.get("weekly_resets")))
