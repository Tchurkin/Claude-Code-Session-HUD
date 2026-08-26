#!/usr/bin/env python3
"""
Subscription usage - the real numbers, not an estimate, plus which way they are heading.

How much of your session you've spent is the one thing the HUD couldn't work out locally. Transcripts
record every token, but a plan's limits are weighted per model and live server-side, so a local tally
can tell you what you burned and never what you have left. Claude Code's own ``/usage`` gets the real
figures from ``/api/oauth/usage``; this asks the same endpoint, with the OAuth token Claude Code has
already stored on this machine.

A percentage on its own is only half the story: 60% spent is comfortable four hours into a window and
alarming twenty minutes in. So this also keeps a short history of readings and fits a burn rate to
them, which turns the bare number into the question you actually care about - at this rate, do I run
out before the window resets? That answer is published as ``pace`` and the meter paints it.

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
POLL_MS     = 60000          # base for the failure backoff, and the cadence when in any doubt
FAIL_MAX_MS = 600000         # ... and when one fails, back off rather than hammering

# How often to ask, by whether anything is actually being spent. A fixed cadence spends the same
# request budget on a frozen number as on a moving one, and this endpoint rate-limits: a day of three
# busy hours and twenty-one idle ones goes from 1440 requests to about 800, while the hours that
# actually move the number get watched more closely rather than less.
POLL_FAST_MS = 45000         # a chat is mid-turn: the number is moving
POLL_WARM_MS = 60000         # unknown, or no rate yet - today's cadence, the safe default
POLL_SLOW_MS = 240000        # chats open, nothing running: still six samples inside RATE_WINDOW_MS
MOVE_EPS     = 0.05          # a rise smaller than this is float noise, not spending

# Fitting the burn rate. Twenty minutes is long enough to ride out the coarseness of a percentage
# that only moves in whole points, and short enough that putting the laptop down shows up in the
# colour within the same window - samples simply age out of it, so the rate decays on its own.
RATE_WINDOW_MS = 20 * 60 * 1000
MIN_SAMPLES    = 4
MIN_SPAN_MS    = 5 * 60 * 1000
MAX_SAMPLES    = 240         # backstop; the window trim is what normally bounds this
WINDOW_DROP    = 3.0         # a fall this size means a new window, not a rounding wobble
WINDOW_TOL_S   = 120.0       # reset times this close are the same window (the API's jitters by ~1s)

# Telling you once, at the moment it becomes true, that this window is going to run out early.
# Two thresholds rather than one: against a single 0.5 boundary a pace sitting at 0.499/0.501 crosses
# up on alternate polls and chatters at you all afternoon. It has to fall back to PACE_CLEAR - a
# properly quieter burn, not a rounding wobble - before it can fire again.
PACE_RED   = 0.5             # above this, the burn runs the window out before it resets
PACE_CLEAR = 0.42            # and it has to come back below this to re-arm


def _token():
    try:
        with open(CREDENTIALS, encoding="utf-8-sig") as f:
            return (json.load(f).get("claudeAiOauth") or {}).get("accessToken")
    except Exception:
        return None


def _util(block):
    """Utilization as a float, keeping whatever resolution the API gave us - the rate fit wants it."""
    try:
        v = block.get("utilization")
        return None if v is None else max(0.0, min(100.0, float(v)))
    except Exception:
        return None


def _pct(block):
    u = _util(block)
    return None if u is None else int(round(u))


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
            "session_pct": _pct(five), "session_util": _util(five),
            "session_resets": five.get("resets_at"),
            "weekly_pct": _pct(week),  "weekly_resets": week.get("resets_at"),
            "severity": sev}


# -- which way it's heading -------------------------------------------------------------------
def mins_until(iso):
    """Minutes until an ISO timestamp, or None if it isn't one. Never negative."""
    if not iso:
        return None
    try:
        from datetime import datetime, timezone
        t = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return max(0.0, (t - datetime.now(timezone.utc)).total_seconds() / 60.0)
    except Exception:
        return None


def same_window(a, b, tol_s=WINDOW_TOL_S):
    """Whether two reset timestamps mean the same window.

    Not string equality, which is the obvious thing and is wrong: the API works resets_at out from
    the instant it is asked, so the sub-second part moves on every single poll while the window has
    not changed at all - 01:00:00.405709, then 01:00:00.034031, then 00:59:59.604980. Comparing the
    text would call every poll a rollover, throw the history away each time, and leave the burn rate
    permanently unknown. A real rollover moves this by hours, so a couple of minutes of slack
    separates the two cleanly."""
    if a == b:
        return True
    if not a or not b:
        return False
    ta, tb = mins_until(a), mins_until(b)
    if ta is None or tb is None:
        return False                                  # unparseable: fall back to the text compare
    return abs(ta - tb) * 60.0 <= tol_s


def _keep(hist, sample, resets, prev_resets):
    """The samples that still describe the window we are in now.

    History is only meaningful within one window: when the window rolls over, utilization drops back
    to near zero and a rate fitted across the seam would read as a huge negative burn. So the history
    is dropped whenever the reset time moves on, and - belt and braces, since the API could round the
    reset time or omit it - whenever the reading itself falls by more than a rounding wobble."""
    ts, util = sample
    hist = [h for h in hist if isinstance(h, (list, tuple)) and len(h) == 2]
    if resets and prev_resets and not same_window(resets, prev_resets):
        hist = []
    elif hist and util < hist[-1][1] - WINDOW_DROP:
        hist = []
    hist = [list(h) for h in hist if h[0] < ts]        # ignore anything at or ahead of now
    hist.append([ts, util])
    hist = [h for h in hist if ts - h[0] <= RATE_WINDOW_MS]
    return hist[-MAX_SAMPLES:]


def burn_rate(hist):
    """Utilization points per minute, by least squares over the history. None if it can't be said.

    Least squares rather than first-minus-last because the reading moves in coarse steps: across
    twenty one-point samples a difference of endpoints is mostly quantization, while the fitted slope
    uses every sample and settles down. Clamped at zero - the reading only climbs within a window, so
    a negative slope is noise, not a refund."""
    pts = [(h[0] / 60000.0, float(h[1])) for h in hist
           if isinstance(h, (list, tuple)) and len(h) == 2]
    if len(pts) < MIN_SAMPLES:
        return None
    if (pts[-1][0] - pts[0][0]) * 60000.0 < MIN_SPAN_MS:
        return None
    n = float(len(pts))
    mt = sum(t for t, _ in pts) / n
    mu = sum(u for _, u in pts) / n
    den = sum((t - mt) ** 2 for t, _ in pts)
    if den <= 0:
        return None
    return max(0.0, sum((t - mt) * (u - mu) for t, u in pts) / den)


def _moving(hist):
    """Did the reading actually rise between the last two samples?

    A second opinion on "is anything being spent", independent of the caller's. The fitted burn rate
    is no good for this: it lags minutes on the way up and keeps a decaying tail for a full twenty
    minutes on the way down, so it would hold the fast cadence long after you stopped."""
    if len(hist) < 2:
        return False
    try:
        return float(hist[-1][1]) - float(hist[-2][1]) > MOVE_EPS
    except Exception:
        return False


def poll_interval(cur, active=None):
    """How long to wait before asking again. Only relaxes when told nothing is running AND a rate
    already exists to protect.

    `active` is the caller's read on whether a chat is mid-turn - instantaneous and free, where the
    burn rate is neither. None means the caller could not tell, which is a reason to hold the normal
    cadence, not to relax.

    The cold-start branch is the important one. burn_rate needs samples spanning five minutes, so how
    soon a rate first exists is bound by that span, not by the sample count: at 60s it converges in
    five minutes, at 240s it would need twelve and be one dropped poll from never converging at all.
    So a missing rate is never a reason to slow down. It also self-heals after a window rollover,
    when _keep wipes the history and burn goes back to None: the cadence snaps to 60s, a rate exists
    again a few polls later, and it relaxes on its own."""
    if active or _moving(cur.get("history") or []):
        return POLL_FAST_MS
    if cur.get("burn") is None:
        return POLL_WARM_MS
    if active is False:
        return POLL_SLOW_MS
    return POLL_WARM_MS


def pace(util, rate, mins_left):
    """Where this window is heading, as 0..1: 0 = coasting, 0.5 = lands exactly on the limit, 1 = out.

    The scale is your burn against what you can still afford - `sustainable` below, the rate that
    spends exactly the rest of the window over exactly the time left in it. Half that rate is 0.25,
    the rate itself is 0.5, and past it you run out early, where the scale becomes how EARLY: hitting
    the limit just as the window closes is that same 0.5, hitting it immediately is 1. The branches
    agree at ratio == 1 so the colour crosses over smoothly.

    Deliberately says nothing about how full the window already is, only about where it is going. How
    full it is is what the bar's own length says, and pricing it into the colour as well would both
    duplicate that and blunt this: idle at 90% would read as a warning when the truthful answer is
    that nothing is being spent. It goes red quickly enough on its own if you resume, because at 90%
    the sustainable rate is tiny and almost any real work is a multiple of it."""
    if util is None:
        return None
    util = max(0.0, min(100.0, float(util)))
    if util >= 100.0:
        return 1.0                                     # already out; nothing left to project
    if rate is None or mins_left is None:
        return None
    rate = max(0.0, float(rate))
    mins_left = max(0.0, float(mins_left))
    if rate <= 0 or mins_left <= 0:
        return 0.0                                     # not burning, or no window left to burn it in
    sustainable = (100.0 - util) / mins_left           # points a minute you can still afford
    ratio = rate / sustainable
    if ratio <= 1.0:
        return max(0.0, min(0.5, 0.5 * ratio))
    hit = (100.0 - util) / rate                        # minutes to the limit; less than mins_left
    return max(0.5, min(1.0, 0.5 + 0.5 * (1.0 - hit / mins_left)))


def _red_edge(prev_hot, p, rolled_over):
    """Latch for "this window is heading over the limit". Returns (hot, fired).

    Fires on the way up and only on the way up, once per crossing. `p is None` is not "below the
    threshold" - it is no claim at all, which happens for the first five minutes of every window and
    any time the history is wiped, and treating it as a fall would re-arm the latch and fire again
    on the way back into a state we were already in."""
    if rolled_over:
        return (False, False)               # new window, new budget: re-arm, and do not fire
    if p is None:
        return (bool(prev_hot), False)      # no rate yet: neither arm nor fire
    if not prev_hot and p > PACE_RED:
        return (True, True)                 # the one moment worth interrupting you for
    if prev_hot and p < PACE_CLEAR:
        return (False, False)               # eased off enough to be worth hearing about again
    return (bool(prev_hot), False)


def project(cur):
    """Work out burn / projected / pace / hit_mins for a reading, in place. Returns it."""
    util = cur.get("session_util")
    if util is None and cur.get("session_pct") is not None:
        util = float(cur["session_pct"])
    left = mins_until(cur.get("session_resets"))
    rate = burn_rate(cur.get("history") or [])
    cur["burn"] = None if rate is None else round(rate, 4)
    cur["pace"] = cur["projected"] = cur["hit_mins"] = None
    if util is None:
        return cur
    p = pace(util, rate, left)
    cur["pace"] = None if p is None else round(p, 4)
    if rate is not None and left is not None:
        cur["projected"] = round(min(999.0, util + rate * left), 1)
        if util >= 100.0:
            cur["hit_mins"] = 0
        elif rate > 0:
            cur["hit_mins"] = int(round((100.0 - util) / rate))
    return cur


# -- cache ------------------------------------------------------------------------------------
def read():
    try:
        with open(CACHE, encoding="utf-8-sig") as f:
            d = json.load(f)
            return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _publish(d):
    try:
        os.makedirs(hc.DATA_DIR, exist_ok=True)
        tmp = CACHE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f)
        os.replace(tmp, CACHE)
    except Exception:
        pass


def _num(d, key):
    try:
        return float(d.get(key) or 0)
    except Exception:
        return 0.0


def refresh(force=False, active=None):
    """Fetch no more often than the cadence deserves, and publish for the overlays to draw.

    `active` says whether a chat is mid-turn; see poll_interval. Returns the current values."""
    cur = read()
    now = time.time() * 1000
    wait = poll_interval(cur, active)                  # from the cache: all we know before fetching
    if not force:
        if now - _num(cur, "ts") < wait:
            return cur
        if now < _num(cur, "next_try"):
            return cur                                 # a recent fetch failed; let the backoff run
    got = fetch()
    if not got:
        # Don't retry on every daemon tick - the daemon comes round every few seconds, and a failing
        # endpoint (an expired token, a 429) would otherwise get hit that often. `ts` is deliberately
        # left alone so the meter can still tell the reading has gone stale; the backoff has its own
        # field, and doubles up to FAIL_MAX_MS.
        # max(), so the two rules compose without fighting: a 429 always outranks being busy, and a
        # failure while idle never retries sooner than we would have polled anyway. The backoff base
        # stays POLL_MS whatever the activity - rebasing it on the fast tier would make the first
        # retry after a 429 come twice as fast, in exactly the case the endpoint asked us to stop.
        n = int(_num(cur, "fail_n")) + 1
        cur["fail_n"] = n
        cur["next_try"] = now + max(wait, min(FAIL_MAX_MS, POLL_MS * (2 ** min(n - 1, 6))))
        _publish(cur)
        return cur                                     # keep showing the last good numbers
    util = got.get("session_util")
    if util is None and got.get("session_pct") is not None:
        util = float(got["session_pct"])
    prior = list(cur.get("history") or [])
    got["history"] = (_keep(prior, [got["ts"], util], got.get("session_resets"),
                            cur.get("session_resets"))
                      if util is not None else prior)
    project(got)
    # The latch has to be carried across here by hand, and this is the only correct place for it.
    # `got` is a fresh dict from fetch(); everything else in the old cache is discarded on every
    # successful poll, so anything written into usage.json from outside refresh() lives exactly
    # until the next fetch. Same reason `history` is carried two lines up.
    rolled = bool(got.get("session_resets") and cur.get("session_resets")
                  and not same_window(got["session_resets"], cur["session_resets"]))
    hot, fired = _red_edge(cur.get("pace_hot"), got.get("pace"), rolled)
    got["pace_hot"] = hot
    if fired:
        got["pace_alert"] = got["ts"]       # unclaimed; whoever raises it calls clear_alert()
    _publish(got)
    return got


def clear_alert():
    """Consume a pending red-crossing alert. One-shot: the next caller sees nothing."""
    cur = read()
    if cur.pop("pace_alert", None) is None:
        return False
    _publish(cur)
    return True


if __name__ == "__main__":
    import sys
    offline = "--no-fetch" in sys.argv                  # inspect the cache without spending a request
    u = project(read()) if offline else refresh(force=True)
    print("session %s%%  (resets %s)" % (u.get("session_pct"), u.get("session_resets")))
    print("weekly  %s%%  (resets %s)" % (u.get("weekly_pct"), u.get("weekly_resets")))
    print("history %d samples   burn %s %%/min   projected %s%%   pace %s   limit in %s min"
          % (len(u.get("history") or []), u.get("burn"), u.get("projected"),
             u.get("pace"), u.get("hit_mins")))
