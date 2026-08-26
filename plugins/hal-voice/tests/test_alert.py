"""Telling you once, at the moment your burn will run the session out early.

The colour on the meter only helps if you are looking at it, and the moment the burn tips into red
is the one instant where easing off still changes the outcome. So it chimes - once. Once is the
whole difficulty: a pace hovering either side of the threshold would otherwise nag all afternoon,
and a window that rolls over must re-arm without announcing itself.

Drives the real refresh() through scripted readings with the network replaced. Nothing sounds,
nothing is drawn, nothing is spawned.
"""
import os, re, sys, tempfile, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import check              # noqa: E402  (also puts scripts/ on the path)

import hal_common as hc                 # noqa: E402
import hal_usage as hu                  # noqa: E402
import hal_sessions as hs               # noqa: E402

SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")


# -- 1. the latch ---------------------------------------------------------------------------------
# Fires on the way up, once. Everything else about it is about NOT firing.
check(hu._red_edge(False, 0.9, False) == (True, True), "crossing up fires")
check(hu._red_edge(True, 0.9, False) == (True, False), "and staying up says nothing further")
check(hu._red_edge(True, 0.45, False) == (True, False),
      "a dip that is still above the re-arm band stays latched")
check(hu._red_edge(True, 0.30, False) == (False, False), "a real fall re-arms, silently")
check(hu._red_edge(False, 0.30, False) == (False, False), "and a quiet window stays quiet")

# Chatter is the failure this exists to prevent: one threshold, and 0.499/0.501 alternates forever.
hot, fires = False, 0
for p in [0.499, 0.501] * 25:
    hot, fired = hu._red_edge(hot, p, False)
    fires += 1 if fired else 0
check(fires == 1, "fifty polls wobbling across the line fire exactly once (got %d)" % fires)
check(hu.PACE_CLEAR < hu.PACE_RED, "the re-arm band is genuinely below the trigger")

# `None` is not "below the threshold" - it is no claim at all, and it happens for the first five
# minutes of every window and any time the history is wiped.
check(hu._red_edge(True, None, False) == (True, False), "no rate does not re-arm a hot latch")
check(hu._red_edge(False, None, False) == (False, False), "nor arm a cold one")
hot, fires = False, 0
for p in [0.9, None, None, 0.9, None, 0.9]:
    hot, fired = hu._red_edge(hot, p, False)
    fires += 1 if fired else 0
check(fires == 1, "a rate that keeps dropping out does not re-fire (got %d)" % fires)

# A new window is a new budget: re-arm, but do not announce it.
check(hu._red_edge(True, 0.9, True) == (False, False), "a rollover re-arms without firing")
check(hu._red_edge(False, 0.9, True) == (False, False), "even from cold")
print("latch: 50 polls across the line -> 1 chime; None never re-arms; rollover is silent")


# -- 2. through the real refresh(), which is where the latch is carried ---------------------------
# The trap this guards: refresh() publishes a dict built fresh by fetch(), so anything written into
# usage.json anywhere else survives exactly until the next successful poll.
tmp = tempfile.mkdtemp(prefix="hud-alert-")
_saved = (hu.CACHE, hu.fetch)
hu.CACHE = os.path.join(tmp, "usage.json")
from datetime import datetime, timedelta, timezone      # noqa: E402

MIN = 60000


def _in(mins):
    """A reset time relative to now. Absolute timestamps here would quietly change what the test
    means as the real clock moved past them - pace depends on how much of the window is left."""
    return (datetime.now(timezone.utc) + timedelta(minutes=mins)).isoformat()


RESETS = _in(120)
LATER = _in(400)


def _feed(util, resets=RESETS, rate=0.0, span_min=20):
    """Publish a reading whose history fits `rate` %/min, then refresh onto it.

    Ramps linearly up TO `util` rather than extrapolating backwards from it: a steep rate would run
    the early samples below zero, and clamping them flat there quietly halves the slope the fit sees.
    """
    now = int(time.time() * 1000)
    lo = max(0.0, util - rate * span_min)
    hist = [[now - (span_min - i) * MIN, lo + (util - lo) * (i / float(span_min))]
            for i in range(span_min + 1)]
    hu.fetch = lambda timeout=10: {"ts": now, "session_pct": int(round(util)),
                                   "session_util": float(util), "session_resets": resets,
                                   "weekly_pct": 5, "weekly_resets": resets, "severity": "normal"}
    cur = hu.read()
    cur["history"] = hist[:-1]
    hu._publish(cur)
    return hu.refresh(force=True)


u = _feed(10.0, rate=0.0)
check(u.get("pace_alert") is None, "a coasting window says nothing")
check(u.get("pace_hot") is False, "and stays armed")

u = _feed(50.0, rate=3.0)                       # far past what the window can afford
check(u.get("pace") > hu.PACE_RED, "a heavy burn is red (pace %r)" % u.get("pace"))
check(u.get("pace_alert"), "which raises an alert")
check(u.get("pace_hot") is True, "and latches")

u = _feed(55.0, rate=3.0)
check(u.get("pace_alert") is None, "still burning does not raise a second one")
check(u.get("pace_hot") is True, "but stays latched")

# The carry-forward is the load-bearing line: without it every poll rebuilds the latch from nothing
# and every poll re-fires.
check(hu.read().get("pace_hot") is True, "and the latch survives a poll, which is the whole point")

u = _feed(56.0, rate=0.0)
check(u.get("pace_hot") is False, "easing right off re-arms")
check(u.get("pace_alert") is None, "silently")

u = _feed(60.0, rate=3.0)
check(u.get("pace_alert"), "and a fresh crossing fires again")

# A window rollover must not inherit the previous window's latch, nor fire on the way in.
u = _feed(2.0, resets=LATER, rate=3.0)
check(u.get("pace_hot") is False, "a new window starts armed")
check(u.get("pace_alert") is None, "and does not announce itself")
print("refresh: latch persists across polls, survives the fetch rebuild, resets with the window")


# -- 3. claiming it is one-shot -------------------------------------------------------------------
_feed(5.0, resets=LATER, rate=0.0)              # settle in the window section 2 rolled us into
_feed(70.0, resets=LATER, rate=3.0)
check(hu.read().get("pace_alert"), "an alert is pending")
check(hu.clear_alert() is True, "the first claimant takes it")
check(hu.clear_alert() is False, "the second gets nothing")
check(hu.read().get("pace_alert") is None, "and it is gone from the file")
check(hu.read().get("pace_hot") is True, "while the latch itself is untouched")
hu.CACHE, hu.fetch = _saved
import shutil                                   # noqa: E402
shutil.rmtree(tmp, ignore_errors=True)
print("claim: one-shot, and claiming does not disturb the latch")


# -- 4. the card belongs to no chat ---------------------------------------------------------------
# `sid` must be a pseudo-chat, not empty: _spawn_popup only writes a pid file when it has a sid, and
# without one a second warning stacks a second card instead of replacing the first.
check(hs.PACE_SID, "the warning card has a sid")
check(hs.PACE_SID == "usage", "which is the documented pseudo-chat")
# Safe only because a real sid8 is the hex prefix of a UUID. 'u', 's' and 'g' are not hex digits.
import hal_badge as hb                           # noqa: E402
check(hb._sid8(hs.PACE_SID) == hs.PACE_SID, "it survives _sid8 unchanged")
check(not re.fullmatch(r"[0-9a-f]{8}", hs.PACE_SID),
      "and cannot collide with a real chat id, which is 8 hex characters")
# And the card is actually spawned with it. Without a sid, _spawn_popup writes no pid file, so a
# second warning stacks a new card beside the old one instead of replacing it.
check(re.search(r"_spawn_popup\((?:.|\n)*?sid=PACE_SID\)",
                open(os.path.join(SCRIPTS, "hal_sessions.py"), encoding="utf-8").read()),
      "the warning card is spawned with that sid, so a second one replaces the first")

# The hover hint has to tell the truth: this card cannot jump anywhere.
popup = open(os.path.join(SCRIPTS, "popup.ps1"), encoding="utf-8").read()
check('"Click to dismiss"' in popup, "popup.ps1 has a hint for a card that cannot jump")
check(re.search(r'\$hint\s*=\s*if\s*\(', popup),
      "and chooses between them rather than always promising a jump")
check('$hint = "Click to jump"' not in popup, "the unconditional promise is gone")

# It must not be mistaken for a tab by anything that walks the badge directory.
check(hs.PACE_SID + ".json" not in open(os.path.join(SCRIPTS, "hal_sessions.py"),
                                        encoding="utf-8").read(),
      "nothing creates a badges/usage.json, so reconcile never sees a phantom tab")
print("card: pseudo-sid %r, cannot collide, and admits it only dismisses" % hs.PACE_SID)


# -- 5. wiring ------------------------------------------------------------------------------------
check(hc._DEFAULTS.get("usage_alert") is True, "the alert is on by default")
sess = open(os.path.join(SCRIPTS, "hal_sessions.py"), encoding="utf-8").read()
check("_IS_DAEMON" in sess and "clear_alert()" in sess, "reconcile claims the alert")
# One regex for the whole shape, because each half matters: gated on _IS_DAEMON so a hook cannot
# chime a second time for the same crossing, and clear_alert() inside the CONDITION so the alert is
# consumed before the card and chime happen - a crash between the two must not re-chime on restart.
check(re.search(r"if\s+_IS_DAEMON\s+and\s+u\.get\(.pace_alert.\)\s+and\s+"
                r"hal_usage\.clear_alert\(\)\s*:\s*\n\s*_warn_pace\(", sess),
      "the daemon claims the alert first and only then raises it")
badge = open(os.path.join(SCRIPTS, "hal_badge.py"), encoding="utf-8").read()
check("def beep_detached" in badge, "the chime can outlive a short-lived caller")
check('"--beep"' in badge, "via the entry point that waits for the sound to finish")
check("beep_detached" in sess, "and that is what the alert uses")
print("wiring: daemon-only, cleared before raised, chime detached from the caller")

print("\nOK - one chime per crossing, and only the moment it becomes true")
