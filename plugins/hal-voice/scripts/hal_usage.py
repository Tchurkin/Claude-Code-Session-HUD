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
AUTH_MAX_MS = 60000          # ... except an expired token, which heals itself the moment you work

# Why the last fetch failed, so the backoff can tell "leave the endpoint alone" from "wait for
# Claude Code to renew the token". They want very different ceilings: a rate limit is asking to be
# left alone for minutes, whereas an expired token is fixed the instant you use Claude again, and
# sitting out ten minutes after that means the meter is stale for no reason.
LAST_ERR = None

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
LONG_EVERY_MS  = 60000       # one sample a minute, kept for the whole window, purely to be drawn
LONG_MAX       = 400         # five hours of them, plus slack
# Smoothing for the drawn rate. The reading only moves in whole points, so the difference between
# one minute and the next is 0, 0, 1, 0, 1 - which is not a rate, it is a staircase. A trailing
# five-minute average resolves 0.2 %/min, which is fine against a window that sustains around 0.33.
RATE_SMOOTH_MS = 5 * 60 * 1000

# Turning local token spend into a live percentage. The endpoint answers every 45s at best and only
# in whole points, so between answers the number is frozen and a burn rate fitted to it needs five
# minutes of samples before it says anything. Tokens are measured here, continuously, and the two
# track each other closely enough to interpolate - so the reading between fetches is the last real
# one plus what we have spent since, and every fetch snaps it back to the truth.
#
# The conversion is never hardcoded: it is fitted from consecutive readings, so it follows the plan
# rather than assuming one. CAL_STEP is the utilization gap a pair must span before it is trusted -
# at ±0.5 of quantization on each end, a 1-point gap is 50% error and a 4-point gap is 12%.
CAL_STEP       = 4.0
CAL_PAIRS      = 8           # how many recent pairs the fit averages over
# Used only until a real fit exists, so the reading is live from the first minute rather than from
# the first time the window happens to move four points. Being wrong here costs almost nothing: the
# extrapolation only ever spans one poll interval, so at a heavy 60k tokens a minute even a rate
# that is twice off mis-states the reading by a third of a point before the next fetch corrects it.
# Measured on this machine at 276k-420k across two independent methods.
PER_PT_DEFAULT = 320000.0

WEEK_MINS      = 7 * 24 * 60
WEEK_MIN_SEEN  = 6 * 60      # don't project a week from its first few hours

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
    globals()["LAST_ERR"] = None
    try:
        req = urllib.request.Request(ENDPOINT, headers={
            "Authorization": "Bearer %s" % tok,
            "Content-Type": "application/json",
            "anthropic-beta": "oauth-2025-04-20",
            "User-Agent": "claude-session-hud",
        })
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read().decode("utf-8"))
    except Exception as e:
        code = getattr(e, "code", None)
        globals()["LAST_ERR"] = ("auth" if code in (401, 403) else
                                 "rate" if code == 429 else "net")
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
def epoch_ms(iso):
    """An ISO timestamp as epoch milliseconds, or None. Not clamped, unlike mins_until."""
    if not iso:
        return None
    try:
        from datetime import datetime, timezone
        t = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return t.timestamp() * 1000.0
    except Exception:
        return None


def mins_until(iso):
    """Minutes until an ISO timestamp, or None if it isn't one. Never negative - a window that has
    already reset has zero left, not a negative amount. Compare timestamps with epoch_ms instead:
    this clamp makes any two past times look identical."""
    t = epoch_ms(iso)
    if t is None:
        return None
    import time as _t
    return max(0.0, (t - _t.time() * 1000.0) / 60000.0)


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
    # epoch_ms, not mins_until: that one clamps at zero, so two windows that have BOTH already
    # reset would compare as identical however far apart they actually were.
    ta, tb = epoch_ms(a), epoch_ms(b)
    if ta is None or tb is None:
        return False                                  # unparseable: fall back to the text compare
    return abs(ta - tb) <= tol_s * 1000.0


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


def _calibrate(cur, util, total, rolled):
    """Fit weighted-tokens-per-utilization-point from consecutive readings, in place.

    Only across a gap wide enough to survive the API's whole-point rounding, and never across a
    window rollover - utilization falls off a cliff there and the pair would be nonsense."""
    if total is None or util is None:
        return
    fu, ft = cur.get("cal_from_util"), cur.get("cal_from_tok")
    if rolled or fu is None or ft is None or util < fu:
        cur["cal_from_util"], cur["cal_from_tok"] = util, total
        return
    du, dt = util - float(fu), total - float(ft)
    if du < CAL_STEP or dt <= 0:
        return                                        # not yet a pair worth learning from
    pairs = [x for x in (cur.get("cal") or []) if isinstance(x, (list, tuple)) and len(x) == 2]
    # Reject a pair that disagrees wildly with what we already believe. One bad reading - a daemon
    # restart that re-tails a transcript, a machine that slept through half a window - would
    # otherwise drag the fit somewhere it takes hours to climb back from.
    ref = PER_PT_DEFAULT
    if pairs:
        ref = sum(x[1] for x in pairs) / max(1e-9, sum(x[0] for x in pairs))
    this = dt / du
    # The low side is the dangerous one, and it is not hypothetical: utilization is the whole plan,
    # so Claude Desktop or the web app move it while spending no tokens we can see here. That reads
    # as "the window moved a long way on very few tokens" and drags the fitted rate down, which then
    # makes the live reading run ahead of the truth. A pair only counts when what we measured
    # locally plausibly accounts for the movement.
    #
    # Guarded against PER_PT_DEFAULT when there are no pairs yet, because the very first pair sets
    # the reference for every later one and had nothing checking it at all.
    if ref > 0 and (this > ref * 3 or this < ref / 2):
        cur["cal_from_util"], cur["cal_from_tok"] = util, total
        return
    pairs.append([round(du, 3), round(dt, 1)])
    cur["cal"] = pairs[-CAL_PAIRS:]
    cur["cal_from_util"], cur["cal_from_tok"] = util, total
    su = sum(x[0] for x in cur["cal"])
    st = sum(x[1] for x in cur["cal"])
    if su > 0 and st > 0:
        cur["per_pt"] = round(st / su, 1)             # weighted tokens per utilization point


def live_util(cur, total):
    """The reading, brought up to date with what has been spent since it was taken.

    Returns None when there is nothing to add to - no calibration yet, or no token figure - so the
    caller falls back to the last measured value rather than to a guess."""
    if total is None:
        return None
    per = cur.get("per_pt") or PER_PT_DEFAULT
    au, at = cur.get("anchor_util"), cur.get("anchor_tok")
    if per <= 0 or au is None or at is None:
        return None
    try:
        extra = (float(total) - float(at)) / float(per)
    except Exception:
        return None
    if extra < 0:
        extra = 0.0                                   # the counter only ever climbs; a fall is a restart
    return max(0.0, min(100.0, float(au) + extra))


def _keep_long(cur, ts, util, rolled):
    """A coarser history than the burn fit needs, kept for the whole window so the panel can draw
    the shape of the session rather than the last twenty minutes of it."""
    lg = [] if rolled else [x for x in (cur.get("long") or [])
                            if isinstance(x, (list, tuple)) and len(x) == 2]
    if util is None:
        return lg
    if not lg or ts - lg[-1][0] >= LONG_EVERY_MS:
        lg.append([ts, util])
    elif util > lg[-1][1]:
        # Within the same minute, keep the higher reading but NOT its timestamp - moving that would
        # push the bucket forward every time and the next minute would never arrive.
        lg[-1] = [lg[-1][0], util]
    return lg[-LONG_MAX:]


def rate_series(long_hist):
    """How fast the window is being spent, over time - not how much of it has gone.

    The bar's own length already says how full it is; a chart of the same thing says it twice and
    answers the wrong question. What you want from a chart is whether you are burning *now*, which
    means it has to sit at zero when nothing is happening. So: the derivative, smoothed, and drawn
    against a floor of zero rather than against its own minimum."""
    pts = [p for p in (long_hist or [])
           if isinstance(p, (list, tuple)) and len(p) == 2]
    out = []
    for i, (ts, v) in enumerate(pts):
        j = i
        while j > 0 and ts - pts[j - 1][0] <= RATE_SMOOTH_MS:
            j -= 1
        mins = (ts - pts[j][0]) / 60000.0
        if mins <= 0:
            out.append([ts, 0.0])                     # no span yet: claim nothing, not infinity
            continue
        out.append([ts, round(max(0.0, (v - pts[j][1]) / mins), 3)])
    return out


def weekly_project(cur):
    """Where the weekly window lands, and when it would run out. Returns (percent, hit_ms).

    From the plain average rate over however much of the week has already elapsed - no calibration,
    no fitted slope. A seven-day window averaged over days is precisely the case where "at the rate
    so far" is a fair projection; the five-hour one needs a fitted burn because a single burst
    dominates it, but nothing dominates a week. Says nothing at all for the first few hours, when
    one busy morning would project a catastrophe."""
    util = cur.get("weekly_pct")
    left = mins_until(cur.get("weekly_resets"))
    if util is None or left is None or left <= 0:
        return None, None
    elapsed = WEEK_MINS - left
    if elapsed < WEEK_MIN_SEEN or util <= 0:
        return None, None
    projected = min(999.0, float(util) * WEEK_MINS / elapsed)
    hit = None
    if projected > 100.0:
        start = epoch_ms(cur.get("weekly_resets"))
        if start is not None:
            start -= WEEK_MINS * 60000.0                  # when this window opened
            hit = int(start + (elapsed * 100.0 / float(util)) * 60000.0)
    return projected, hit


def project(cur):
    """Work out burn / projected / pace / hit_mins for a reading, in place. Returns it."""
    util = cur.get("session_util")
    if util is None and cur.get("session_pct") is not None:
        util = float(cur["session_pct"])
    left = mins_until(cur.get("session_resets"))
    rate = burn_rate(cur.get("history") or [])
    cur["burn"] = None if rate is None else round(rate, 4)
    cur["pace"] = cur["projected"] = cur["hit_mins"] = None
    # Where the week lands, and the tokens-per-point default. Both published rather than duplicated
    # in PowerShell: a constant kept in two languages is one that will disagree with itself, which is
    # exactly how the daemon came to beat slower than its own staleness threshold and look dead.
    # Burn we measured against burn we can explain. Utilization is the whole plan, not just Claude
    # Code on this machine, so the difference is real spend from somewhere with no transcript here -
    # the desktop app, the web app, another machine. Expressed in the same weighted-tokens-a-minute
    # the per-chat rows use, so it can sit among them and be compared at a glance.
    per = cur.get("per_pt") or PER_PT_DEFAULT
    seen = cur.get("tpm_seen")
    cur["elsewhere"] = None
    if rate is not None and seen is not None and per > 0:
        gap = rate * per - float(seen)
        # A fitted rate against a windowed average will not agree exactly; only claim a gap that is
        # both large in absolute terms and a real share of the total, never a rounding difference.
        if gap > 20000 and gap > 0.25 * rate * per:
            cur["elsewhere"] = int(round(gap))
    wp, wh = weekly_project(cur)
    cur["weekly_projected"] = int(round(wp)) if wp is not None else None
    cur["weekly_hit"] = wh
    cur["per_pt_default"] = PER_PT_DEFAULT
    cur["rates"] = rate_series(cur.get("long"))
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


def refresh(force=False, active=None, tok_total=None, tpm=None):
    """Fetch no more often than the cadence deserves, and publish for the overlays to draw.

    `active` says whether a chat is mid-turn; see poll_interval. `tok_total` is the running count of
    weighted tokens from hal_tokens, passed in rather than imported so this module keeps depending on
    nothing but hal_common. Returns the current values."""
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
        ceiling = AUTH_MAX_MS if LAST_ERR == "auth" else FAIL_MAX_MS
        cur["next_try"] = now + max(wait, min(ceiling, POLL_MS * (2 ** min(n - 1, 6))))
        cur["err"] = LAST_ERR
        # The window we last measured may have ended while we were unable to ask about it. Then the
        # number we are holding is not merely old, it is wrong, and we can say so without the API.
        _infer_rollover(cur, now, active)
        _publish(cur)
        return cur                                     # keep showing the last good numbers
    util = got.get("session_util")
    if util is None and got.get("session_pct") is not None:
        util = float(got["session_pct"])
    prior = list(cur.get("history") or [])
    got["history"] = (_keep(prior, [got["ts"], util], got.get("session_resets"),
                            cur.get("session_resets"))
                      if util is not None else prior)
    # Same test _keep uses, and for the same reason: when we do not know which window the previous
    # reading belonged to, assume it was a different one. Requiring BOTH sides to be present missed
    # the case that matters - _infer_rollover clears session_resets precisely BECAUSE the window
    # rolled, so the next real fetch saw a missing previous window and decided nothing had changed.
    # The chart then spanned two windows and drew the rollover as a cliff in the middle of it.
    rolled_w = not same_window(got.get("session_resets"), cur.get("session_resets"))
    for k in ("cal", "cal_from_util", "cal_from_tok", "per_pt", "anchor_util", "anchor_tok"):
        if k in cur:
            got[k] = cur[k]                            # fetch() builds a fresh dict; carry these over
    got["tpm_seen"] = tpm
    _calibrate(got, util, tok_total, rolled_w)
    got["anchor_util"], got["anchor_tok"] = util, tok_total
    got["long"] = _keep_long(cur, got["ts"], util, rolled_w)
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


def _infer_rollover(cur, now, active):
    """A five-hour window that has passed its reset is spent and gone: the next one starts empty.

    Worth doing without the API because the failure that keeps us from asking - a token that expired
    while the machine slept - is exactly the failure that lasts long enough for a window to roll
    over underneath it. Holding up last night's 40% is worse than useless; it is a specific wrong
    number, and greying it out only says "this is old", not "this is no longer true".

    Only when nothing is running. If a chat is mid-turn we are being spent right now, and the honest
    answer is that we do not know how much - which is what stale already means."""
    if active:
        return False
    left = mins_until(cur.get("session_resets"))
    if left is None or left > 0:
        return False
    # `long` goes with `history`. It is the window's readings, and this window is over - keeping them
    # left the chart spanning two windows and, worse, joining the last reading before you stopped to
    # the first zero of the next one. The sparkline draws straight lines between samples, so ten idle
    # hours across a rollover came out as one long diagonal that reads as a slow decline. Utilization
    # does not decay while you are idle; it holds flat and then falls off a cliff at the reset.
    cur.update({"session_pct": 0, "session_util": 0.0, "session_resets": None,
                "history": [], "long": [], "burn": None, "pace": 0.0, "projected": 0,
                "hit_mins": None, "pace_hot": False})
    wl = mins_until(cur.get("weekly_resets"))
    if wl is not None and wl <= 0:
        cur.update({"weekly_pct": 0, "weekly_resets": None})
    # `ts` moves, because this IS current knowledge rather than an old reading - but `inferred`
    # marks it so the meter can say where the number came from, and any real fetch clears it.
    cur["ts"] = int(now)
    cur["inferred"] = True
    return True


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
