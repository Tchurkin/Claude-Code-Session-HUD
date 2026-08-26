"""The session bar's colour is a claim about the future, so the arithmetic behind it has to hold.

Covers the whole chain: keeping a history that belongs to one window, fitting a burn rate to coarse
readings, turning that into the 0..1 `pace` the meter paints, and backing off when the endpoint says
no. The last section pulls the real colour ramp out of hal_meter.ps1 and runs it, because a ramp that
looked right when it was written is exactly the kind of thing a later tidy-up quietly ruins.

Nothing here touches the real usage cache or the network - `fetch` is replaced throughout.
"""
import os, re, shutil, subprocess, sys, tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import check                # noqa: E402  (also puts scripts/ on the path)

import hal_usage as hu                    # noqa: E402

MIN = 60000


def _hist(start_ms, minutes_and_utils):
    return [[start_ms + m * MIN, u] for m, u in minutes_and_utils]


# -- 1. history belongs to one window ----------------------------------------------------------
T0 = 1_700_000_000_000

h = _hist(T0, [(0, 10.0), (1, 11.0), (2, 12.0)])
kept = hu._keep(list(h), [T0 + 3 * MIN, 13.0], "reset-A", "reset-A")
check(len(kept) == 4, "a sample inside the same window is appended (got %d)" % len(kept))

kept = hu._keep(list(h), [T0 + 3 * MIN, 0.5], "reset-B", "reset-A")
check(kept == [[T0 + 3 * MIN, 0.5]],
      "the reset time moving on drops everything before it (got %r)" % kept)

# The API recomputes resets_at from the instant it is asked, so the same window arrives with a
# slightly different timestamp every minute. These three are real consecutive readings. Comparing
# them as text made every poll look like a rollover, which wiped the history each time and left the
# burn rate permanently unknown - the meter fell back to thresholds forever and looked fine doing it.
JITTER = ["2026-08-26T01:00:00.405709+00:00",
          "2026-08-26T01:00:00.034031+00:00",
          "2026-08-26T00:59:59.604980+00:00"]
for a in JITTER:
    for b in JITTER:
        check(hu.same_window(a, b), "%s and %s are the same window" % (a, b))
check(not hu.same_window(JITTER[0], "2026-08-26T06:00:00.000000+00:00"),
      "but five hours later is genuinely the next one")

acc = []
for i, iso in enumerate(JITTER * 3):
    prev = JITTER[(i - 1) % len(JITTER)] if i else None
    acc = hu._keep(acc, [T0 + i * MIN, 10.0 + i], iso, prev)
check(len(acc) == 9, "so nine jittering polls accumulate nine samples, not one (got %d)" % len(acc))
check(hu.burn_rate(acc) is not None, "and a rate can actually be fitted to them")

# A light day: the window rolls over from 2% to 1%. Far too small a fall for the cliff below to
# notice, so the reset time is the only thing that gives it away - and a leftover sample from the
# old window would sit in the fit as if it were this one's.
kept = hu._keep(_hist(T0, [(0, 1.5), (1, 2.0)]), [T0 + 2 * MIN, 1.0], "reset-B", "reset-A")
check(kept == [[T0 + 2 * MIN, 1.0]],
      "a quiet rollover is caught by the reset time alone (got %r)" % kept)

# Same reset string, but the reading itself fell off a cliff: still a new window.
kept = hu._keep(list(h), [T0 + 3 * MIN, 0.5], "reset-A", "reset-A")
check(kept == [[T0 + 3 * MIN, 0.5]], "a big drop drops the history too (got %r)" % kept)

# A rounding wobble is not a new window.
kept = hu._keep(_hist(T0, [(0, 12.0), (1, 12.4)]), [T0 + 2 * MIN, 11.9], "reset-A", "reset-A")
check(len(kept) == 3, "a sub-threshold dip keeps the history (got %d)" % len(kept))

# Anything older than the fit window is trimmed away.
old = _hist(T0, [(m, float(m)) for m in range(0, 60)])
kept = hu._keep(old, [T0 + 60 * MIN, 60.0], "reset-A", "reset-A")
span = (kept[-1][0] - kept[0][0])
check(span <= hu.RATE_WINDOW_MS, "history is trimmed to the fit window (span %d ms)" % span)
check(len(kept) == hu.RATE_WINDOW_MS // MIN + 1, "and keeps every sample inside it (%d)" % len(kept))

# A clock that jumped backwards must not leave samples ahead of "now".
kept = hu._keep(_hist(T0, [(0, 1.0), (5, 2.0)]), [T0 + 2 * MIN, 3.0], "reset-A", "reset-A")
check(all(k[0] <= T0 + 2 * MIN for k in kept), "no sample survives from ahead of the new one")
print("history: window changes, drops, trims and clock jumps all handled")


# -- 2. the burn rate ---------------------------------------------------------------------------
check(hu.burn_rate([]) is None, "no history, no rate")
check(hu.burn_rate(_hist(T0, [(0, 1.0), (1, 2.0)])) is None, "two samples is not a rate")
check(hu.burn_rate(_hist(T0, [(0, 1.0), (1, 2.0), (2, 3.0), (3, 4.0)])) is None,
      "four samples over three minutes is too short a span to fit")

clean = _hist(T0, [(m, 10.0 + 0.5 * m) for m in range(0, 21)])
r = hu.burn_rate(clean)
check(abs(r - 0.5) < 1e-9, "a clean 0.5 %%/min line fits exactly (got %r)" % r)

# The real signal arrives as whole percentage points; the fit has to see through the steps.
quantized = _hist(T0, [(m, float(int(10 + 0.5 * m))) for m in range(0, 21)])
r = hu.burn_rate(quantized)
check(abs(r - 0.5) < 0.05, "and within 0.05 of it when rounded to whole points (got %.3f)" % r)

flat = _hist(T0, [(m, 42.0) for m in range(0, 21)])
check(hu.burn_rate(flat) == 0.0, "an idle window burns nothing")

falling = _hist(T0, [(m, 50.0 - 0.1 * m) for m in range(0, 21)])
check(hu.burn_rate(falling) == 0.0, "a negative slope is noise, not a refund")

stacked = [[T0, 1.0], [T0, 2.0], [T0, 3.0], [T0, 4.0]]
check(hu.burn_rate(stacked) is None, "samples all at one instant have no slope")
check(hu.burn_rate([[T0, 1.0], "junk", None, [T0 + 9 * MIN, 2.0]]) is None,
      "malformed entries are dropped rather than raising")
print("burn rate: %.3f %%/min fitted through whole-point steps" % hu.burn_rate(quantized))


# -- 3. pace: where the window is heading -------------------------------------------------------
check(hu.pace(30.0, None, 120.0) is None, "no rate yet means no claim about the future")
check(hu.pace(None, 0.5, 120.0) is None, "and no reading means no claim either")
check(hu.pace(100.0, None, None) == 1.0, "a spent window is fully red however little else is known")

# Idling is blue at any percentage. The colour answers "am I going to run out of this window", and
# with nothing being spent the answer is no however little is left. How much is already gone is what
# the bar's own length says; pricing it into the colour as well would duplicate the bar and blunt
# the colour, which would then be unable to say "nothing is happening" about a window where nothing
# is happening.
for used in (5.0, 30.0, 90.0, 99.0):
    p = hu.pace(used, 0.0, 120.0)
    check(p == 0.0, "idle at %g%% is fully blue (got %r)" % (used, p))
check(hu.pace(90.0, 0.0, 5.0) == 0.0, "and so is a window minutes from resetting")

# Green means "spending it exactly", whatever has already gone: the rate that lands you on the limit
# as the window closes is the same statement about the future at 40% used as at 90%.
for used, left in ((40.0, 120.0), (90.0, 120.0), (10.0, 30.0), (99.0, 300.0)):
    sustainable = (100.0 - used) / left
    p = hu.pace(used, sustainable, left)
    check(abs(p - 0.5) < 1e-9,
          "%g%% used at %.4f%%/min lands exactly on the limit (got %r)" % (used, sustainable, p))

# Dropping that floor is only safe because the colour catches up fast when work resumes: at 90% the
# sustainable rate is tiny, so ordinary work is a large multiple of it. Idle for twenty minutes, then
# burn at 0.5%/min, and the fit has to carry the colour past the crossover within a few minutes.
def _resume(t):
    """Twenty-one one-minute samples: flat at 90%, then burning for the last `t` of them."""
    return [[i * MIN, min(100.0, 90.0 + 0.5 * max(0, t - (20 - i)))] for i in range(21)]

crossed = next((t for t in range(0, 13)
                if (hu.pace(_resume(t)[-1][1], hu.burn_rate(_resume(t)), 120.0) or 0.0) > 0.5), None)
check(crossed is not None and crossed <= 6,
      "resuming work at 90%% goes past the crossover within 6 minutes (took %r)" % crossed)

# The crossover: landing exactly on the limit as the window resets is 0.5 from either side.
exact = hu.pace(40.0, 60.0 / 120.0, 120.0)                 # 40 + 0.5*120 = 100
check(abs(exact - 0.5) < 1e-9, "landing exactly on the limit is the green midpoint (got %r)" % exact)
just_under = hu.pace(40.0, (60.0 / 120.0) * 0.999, 120.0)
just_over  = hu.pace(40.0, (60.0 / 120.0) * 1.001, 120.0)
check(just_under < exact <= just_over, "and the two branches meet without a jump")
check(abs(just_over - just_under) < 0.01,
      "the seam is smooth (%.4f -> %.4f)" % (just_under, just_over))

# Over the limit, pace measures how EARLY you run out.
half = hu.pace(0.0, 100.0 / 60.0, 120.0)                   # hits 100 at t=60 of a 120 min window
check(abs(half - 0.75) < 1e-9, "running out halfway through is three-quarters along (got %r)" % half)
soon = hu.pace(0.0, 100.0 / 6.0, 120.0)                    # hits 100 after 6 of 120 minutes
check(soon > 0.9, "running out almost at once is hard red (got %r)" % soon)
check(hu.pace(99.9, 50.0, 120.0) <= 1.0, "pace never exceeds 1")

# Monotonic in the burn rate, which is the property the gradient depends on.
prev = -1.0
for rate in [0.0, 0.1, 0.25, 0.5, 0.75, 1.0, 2.0, 5.0, 20.0]:
    p = hu.pace(40.0, rate, 120.0)
    check(p >= prev - 1e-12, "pace rises with the burn rate (%r at %.2f after %r)" % (p, rate, prev))
    prev = p
check(prev >= 0.98, "a wild burn rate saturates the ramp (got %r)" % prev)

# A window about to roll over cannot be blown, however hard you are burning.
check(hu.pace(60.0, 10.0, 0.0) == 0.0, "no time left means no time to overspend")
print("pace: idle is blue at any %%, crossover at 0.5000, monotonic in burn, red %d min after resuming"
      % crossed)


# -- 4. mins_until and the end-to-end projection ------------------------------------------------
from datetime import datetime, timedelta, timezone      # noqa: E402

check(hu.mins_until(None) is None, "no reset time, no countdown")
check(hu.mins_until("not a date") is None, "and garbage does not raise")
future = (datetime.now(timezone.utc) + timedelta(minutes=90)).isoformat()
m = hu.mins_until(future)
check(89 < m < 91, "90 minutes out reads as 90 (got %r)" % m)
past = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
check(hu.mins_until(past) == 0.0, "an elapsed window reads as zero, never negative")
naive = (datetime.now(timezone.utc) + timedelta(minutes=45)).replace(tzinfo=None).isoformat()
check(44 < hu.mins_until(naive) < 46, "a naive timestamp is read as UTC")

now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
cur = {"session_pct": 40, "session_util": 40.0, "session_resets": future,
       "history": [[now_ms - m * MIN, 40.0 - 0.5 * m] for m in range(20, -1, -1)]}
hu.project(cur)
check(abs(cur["burn"] - 0.5) < 0.01, "the projection fits the same rate (got %r)" % cur["burn"])
check(cur["projected"] > 80, "and carries it out to the reset (got %r)" % cur["projected"])
check(0 < cur["pace"] < 0.5, "40%% heading for ~85%% is still under the limit (got %r)" % cur["pace"])
check(cur["hit_mins"] == 120, "with the limit two hours off (got %r)" % cur["hit_mins"])

bare = hu.project({"session_pct": 12, "session_util": 12.0, "session_resets": future, "history": []})
check(bare["pace"] is None and bare["burn"] is None,
      "a cold start makes no claim at all rather than a made-up one")
print("projection: burn %.2f %%/min -> %.1f%% at reset, pace %.3f"
      % (cur["burn"], cur["projected"], cur["pace"]))


# -- 5. a failing endpoint must not be hammered -------------------------------------------------
tmp = tempfile.mkdtemp(prefix="hud-usage-")
_saved = (hu.CACHE, hu.fetch)
hu.CACHE = os.path.join(tmp, "usage.json")
calls = []
hu.fetch = lambda timeout=10: calls.append(1) or None       # always fails

hu.refresh()
check(len(calls) == 1, "the first attempt goes out")
c = hu.read()
check(c.get("fail_n") == 1 and c.get("next_try"), "and the failure is recorded with a backoff")
for _ in range(20):
    hu.refresh()                                            # the daemon, coming round every few sec
check(len(calls) == 1, "but a failing endpoint is not retried on every tick (got %d calls)" % len(calls))

import time                                                 # noqa: E402

waits = []
for n in range(0, 7):
    hu._publish({"fail_n": n})                              # pretend n failures have already piled up
    t0 = time.time() * 1000
    hu.refresh()
    waits.append(int(round((hu.read()["next_try"] - t0) / 1000.0)))
check(waits[:4] == [60, 120, 240, 480],
      "each further failure waits twice as long (got %r)" % waits)
check(waits[4:] == [600, 600, 600],
      "up to a ceiling, so a dead endpoint settles at one try per %d min (got %r)"
      % (hu.FAIL_MAX_MS // 60000, waits))

ok = {"ts": int(datetime.now(timezone.utc).timestamp() * 1000), "session_pct": 7,
      "session_util": 7.0, "session_resets": future, "weekly_pct": 2,
      "weekly_resets": future, "severity": "normal"}
hu.fetch = lambda timeout=10: dict(ok)
hu.refresh(force=True)
c = hu.read()
check(not c.get("fail_n") and not c.get("next_try"),
      "a good reading clears the backoff entirely (got %r)" % {k: c.get(k) for k in ("fail_n", "next_try")})
check(len(c.get("history") or []) == 1, "and starts a fresh history")
hu.CACHE, hu.fetch = _saved
print("backoff: 21 daemon ticks against a dead endpoint -> 1 request, doubling wait, cleared on success")


# -- 6. how often to ask -------------------------------------------------------------------------
# A fixed cadence spends the same request budget on a frozen number as on a moving one, and this
# endpoint rate-limits - we have seen it 429. poll_interval is pure, so most of this is a table.
WITH_RATE = {"burn": 0.3, "history": [[T0, 10.0], [T0 + MIN, 10.0]]}
NO_RATE = {"burn": None, "history": []}

check(hu.poll_interval(WITH_RATE, True) == hu.POLL_FAST_MS, "a chat mid-turn is watched closely")
check(hu.poll_interval(WITH_RATE, False) == hu.POLL_SLOW_MS, "nothing running relaxes")
check(hu.poll_interval(WITH_RATE, None) == hu.POLL_WARM_MS,
      "a caller that cannot tell holds the normal cadence rather than relaxing")

# The cold-start trap: relax before a rate exists and you never accumulate enough to get one.
check(hu.poll_interval(NO_RATE, False) == hu.POLL_WARM_MS,
      "a missing rate is never a reason to slow down, even when idle")
check(hu.poll_interval(NO_RATE, None) == hu.POLL_WARM_MS, "nor when the caller cannot tell")
check(hu.poll_interval(NO_RATE, True) == hu.POLL_FAST_MS, "but real work still wins")

# Second opinion, in case the activity signal is wrong: a reading that visibly rose polls fast
# whatever the caller believes. Not derived from `burn`, which lags going up and trails going down.
check(hu.poll_interval({"burn": 0.3, "history": [[T0, 10.0], [T0 + MIN, 11.0]]}, False)
      == hu.POLL_FAST_MS, "a reading that just rose overrides a caller claiming nothing is running")
check(not hu._moving([[T0, 10.0], [T0 + MIN, 10.0]]), "a flat pair is not movement")
check(not hu._moving([[T0, 10.0], [T0 + MIN, 10.0 + hu.MOVE_EPS / 2]]), "nor is float noise")
check(not hu._moving([[T0, 10.0]]), "one sample cannot show movement")
check(not hu._moving([[T0, 1.0], "junk"]), "and malformed history does not raise")

# The slow tier has to leave the burn fit something to work with. If the fit goes None the meter
# falls back to the plain thresholds, which would paint an idle 90% window RED - the exact
# misreading pace exists to prevent. Ceiling is RATE_WINDOW_MS / (MIN_SAMPLES - 1).
ceiling = hu.RATE_WINDOW_MS / (hu.MIN_SAMPLES - 1)
check(hu.POLL_SLOW_MS <= ceiling,
      "the slow tier stays under the fit ceiling (%d vs %d ms)" % (hu.POLL_SLOW_MS, ceiling))
kept = hu.RATE_WINDOW_MS // hu.POLL_SLOW_MS + 1
check(kept >= hu.MIN_SAMPLES + 2,
      "with margin for two dropped polls (%d samples, fit needs %d)" % (kept, hu.MIN_SAMPLES))
check(hu.burn_rate([[T0 + i * hu.POLL_SLOW_MS, 10.0 + 0.1 * i] for i in range(kept)]) is not None,
      "and a rate really does fit samples spaced that far apart")
check(hu.POLL_SLOW_MS * 3 < 900000,
      "a reading taken at the slow cadence never looks stale to the meter (greys at 15 min)")

# Cadence and backoff compose as max(): a 429 outranks being busy, and a failure while idle never
# retries sooner than we would have polled anyway.
_c = hu.CACHE
hu.CACHE = os.path.join(tmp, "cadence.json")
_f = hu.fetch
hu.fetch = lambda timeout=10: None
hu._publish({"burn": 0.3, "history": [[T0, 10.0], [T0 + MIN, 10.0]], "fail_n": 0})
_t = time.time() * 1000
hu.refresh(active=False)
waited = hu.read()["next_try"] - _t
check(waited >= hu.POLL_SLOW_MS - 100,
      "an idle failure waits the idle cadence, not the 60s backoff step (%d ms)" % waited)
hu._publish({"burn": 0.3, "history": [[T0, 10.0], [T0 + MIN, 11.0]], "fail_n": 5})
_t = time.time() * 1000
hu.refresh(active=True)
waited = hu.read()["next_try"] - _t
check(waited >= 600000 - 100,
      "and a hard-backed-off failure is not shortened by being busy (%d ms)" % waited)
# And refresh() has to actually USE the interval rather than merely compute it - a table of tiers
# that nothing consults would pass everything above and change nothing. Seed a reading 90 seconds
# old: still fresh at the idle cadence, already stale at the busy one and at the old fixed minute.
_calls = []
hu.fetch = lambda timeout=10: _calls.append(1) or None
_seed = {"ts": time.time() * 1000 - 90000, "burn": 0.3,
         "history": [[T0, 10.0], [T0 + MIN, 10.0]]}
hu._publish(dict(_seed))
hu.refresh(active=False)
check(len(_calls) == 0, "a 90s-old reading is still fresh at the idle cadence, so nothing goes out")
hu._publish(dict(_seed))
hu.refresh(active=True)
check(len(_calls) == 1, "but stale at the busy cadence, so the same reading is refetched")

hu.CACHE, hu.fetch = _c, _f
print("cadence: fast %ds / warm %ds / slow %ds, %d samples still fit at the slow tier"
      % (hu.POLL_FAST_MS / 1000, hu.POLL_WARM_MS / 1000, hu.POLL_SLOW_MS / 1000, kept))


# -- 7. the colour ramp the meter actually ships ------------------------------------------------
# Pulled straight out of hal_meter.ps1 rather than restated here: the point is to catch the ramp
# being changed, and a copy in the test would happily keep agreeing with itself.
METER = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "scripts", "hal_meter.ps1")
src = open(METER, encoding="utf-8").read()
m = re.search(r"(\$script:PaceStops\s*=\s*@\(.*?^\}\s*$)", src, re.S | re.M)
check(m is not None, "found the PaceStops table and PaceColor in hal_meter.ps1")
snippet = m.group(1)
check("function PaceColor" in snippet, "the extracted block is the colour ramp")

probe = os.path.join(tmp, "ramp.ps1")
with open(probe, "w", encoding="utf-8") as f:
    f.write("Add-Type -AssemblyName System.Drawing\n")
    f.write(snippet + "\n")
    f.write("foreach ($i in 0..100) {\n"
            "  $c = PaceColor ($i/100.0) $false\n"
            "  Write-Output ('{0} {1} {2} {3}' -f $i, $c.R, $c.G, $c.B)\n}\n")
r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-File", probe],
                   capture_output=True, text=True)
check(r.returncode == 0, "the ramp runs under PowerShell: %s" % (r.stderr or "")[:400])
ramp = {}
for line in r.stdout.split("\n"):
    p = line.split()
    if len(p) == 4:
        ramp[int(p[0])] = tuple(int(x) for x in p[1:])
check(len(ramp) == 101, "got a colour for every step (%d)" % len(ramp))

check(ramp[0]   == (60, 150, 255), "coasting is blue (got %r)" % (ramp[0],))
check(ramp[50]  == (0, 205, 120),  "the crossover is the same green as elsewhere (got %r)" % (ramp[50],))
check(ramp[75]  == (255, 176, 0),  "the caution step is amber (got %r)" % (ramp[75],))
check(ramp[100] == (240, 80, 70),  "running out at once is red (got %r)" % (ramp[100],))

worst_jump = max(max(abs(a - b) for a, b in zip(ramp[i], ramp[i + 1])) for i in range(100))
check(worst_jump <= 12, "the ramp is continuous, no visible banding (worst step %d)" % worst_jump)

# Blue drains away as you approach the limit and red comes up. Neither may double back.
for i in range(50):
    check(ramp[i + 1][2] <= ramp[i][2] + 1, "blue only recedes towards the crossover (step %d)" % i)
for i in range(50, 100):
    check(ramp[i + 1][0] >= ramp[i][0] - 1, "red only builds past the crossover (step %d)" % i)

# The reason the ramp goes through amber: blending green straight into red passes through a dull
# olive, which reads as "off" rather than "urgent" on a dark HUD. Nothing on the ramp may be that.
def sat(c):
    return 0.0 if max(c) == 0 else (max(c) - min(c)) / float(max(c))
worst_sat, worst_at = min((sat(c), i) for i, c in ramp.items())
check(worst_sat >= 0.55, "no muddy step anywhere on the ramp (worst %.2f at pace %.2f)"
                         % (worst_sat, worst_at / 100.0))
olive = tuple((a + b) // 2 for a, b in zip((0, 205, 120), (240, 80, 70)))
check(sat(olive) < 0.55, "...and that floor is what a direct green-to-red blend would fail (%.2f)"
                         % sat(olive))
print("ramp: 101 steps, worst jump %d/255, worst saturation %.2f at pace %.2f"
      % (worst_jump, worst_sat, worst_at / 100.0))

# -- 8. durations stay in units you can picture --------------------------------------------------
# The weekly window is routinely six days out. "150h 12m" is a number you have to divide before it
# means anything; "6d 6h" is a length of time. Same extract-and-run approach as the ramp.
def _ps_function(name):
    """One PowerShell function out of hal_meter.ps1, by matching its braces."""
    i = src.index("function %s" % name)
    depth, j = 0, src.index("{", i)
    for k in range(j, len(src)):
        if src[k] == "{":
            depth += 1
        elif src[k] == "}":
            depth -= 1
            if depth == 0:
                return src[i:k + 1]
    raise AssertionError("unbalanced braces reading %s out of hal_meter.ps1" % name)


probe2 = os.path.join(tmp, "durations.ps1")
with open(probe2, "w", encoding="utf-8") as f:
    f.write(_ps_function("MinsLong") + "\n" + _ps_function("ResetShort") + "\n")
    f.write("foreach ($m in @(0,5,59,60,90,100,155,1439,1440,1500,9000,10079)) "
            "{ Write-Output ('{0}|{1}' -f $m, (MinsLong $m)) }\n")
    f.write("$iso = [datetime]::UtcNow.AddMinutes(9000).ToString('o')\n"
            "Write-Output ('short|{0}' -f (ResetShort $iso))\n")
r2 = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-File", probe2],
                    capture_output=True, text=True)
check(r2.returncode == 0, "the duration helpers run: %s" % (r2.stderr or "")[:400])
got = dict(l.split("|", 1) for l in r2.stdout.strip().splitlines() if "|" in l)

check(got["0"] == "any moment", "zero reads as 'any moment' (got %r)" % got["0"])
check(got["59"] == "59 min", "under an hour stays in minutes (got %r)" % got["59"])
check(got["90"] == "1h 30m", "under a day is hours and minutes (got %r)" % got["90"])
# PowerShell's [int] rounds to nearest, so these two are where a truncating cast is mandatory.
check(got["100"] == "1h 40m", "an hour and forty is not two hours (got %r)" % got["100"])
check(got["155"] == "2h 35m", "nor two thirty-five three hours (got %r)" % got["155"])
check(got["1440"] == "1d 0h", "a full day rolls into days (got %r)" % got["1440"])
check(got["9000"] == "6d 6h", "and 150 hours is six and a bit days (got %r)" % got["9000"])
check(got["10079"] == "6d 23h", "just under a week (got %r)" % got["10079"])
check(got["short"] == "6d 6h", "the compact reading form agrees (got %r)" % got["short"])

# The actual complaint: no reading may ever show two dozen hours or more.
for k, v in got.items():
    m = re.match(r"^(\d+)h", v)
    check(not m or int(m.group(1)) < 24, "%s minutes rendered as %r, which is 24h or more" % (k, v))
print("durations: %s, %s, %s - nothing over 23h" % (got["90"], got["1440"], got["9000"]))

# -- 9. the sparkline's scaling ------------------------------------------------------------------
# The chart spans the samples' own range, which is the only way twenty quiet minutes are visible at
# all - but auto-scaling a signal that only moves in whole points would turn one rounding tick into
# a cliff. SparkNorm comes out of hal_meter.ps1 and gets run, same as the ramp above.
_ps = [
    "function Show($label, $vals, $span) {",
    "  $r = SparkNorm $vals $span",
    "  Write-Output ('{0}|{1}' -f $label, (($r | ForEach-Object { '{0:F3}' -f $_ }) -join ','))",
    "}",
    "Show 'rising' @(10.0,11.0,12.0,13.0) 2.5",
    "Show 'flat'   @(42.0,42.0,42.0,42.0) 2.5",
    "Show 'tick'   @(38.0,38.0,39.0,39.0) 2.5",
    "Show 'wide'   @(10.0,20.0,30.0,40.0) 2.5",
    "Show 'single' @(7.0) 2.5",
    "Show 'dip'    @(50.0,60.0,55.0,70.0) 2.5",
]
probe3 = os.path.join(tmp, "spark.ps1")
with open(probe3, "w", encoding="utf-8") as f:
    f.write(_ps_function("SparkNorm") + "\n" + "\n".join(_ps) + "\n")
r3 = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-File", probe3],
                    capture_output=True, text=True)
check(r3.returncode == 0, "SparkNorm runs under PowerShell: %s" % (r3.stderr or "")[:400])
sn = {}
for line in r3.stdout.strip().splitlines():
    if "|" in line:
        k, v = line.split("|", 1)
        sn[k] = [float(x) for x in v.split(",") if x.strip()]
check(len(sn) == 6, "a series came back for each case (got %r)" % sorted(sn))

check(abs(sn["rising"][0]) < 1e-9 and abs(sn["rising"][-1] - 1.0) < 1e-9,
      "a rising run spans the full height (got %r)" % sn["rising"])
check(sn["rising"] == sorted(sn["rising"]), "and stays monotonic (got %r)" % sn["rising"])

# The one that matters: an unchanging window draws down the MIDDLE. Along the bottom is what 0..100
# scaling would give, and it reads as "you have used nothing" when you may have used almost all.
check(all(abs(v - 0.5) < 1e-9 for v in sn["flat"]),
      "an unchanging window is a flat line through the centre (got %r)" % sn["flat"])
check(len(sn["flat"]) == 4, "and keeps every sample (got %d)" % len(sn["flat"]))

# A single whole-point tick has to stay a modest step; unfloored auto-scaling makes it full height.
rise = max(sn["tick"]) - min(sn["tick"])
check(0.2 < rise < 0.55,
      "a lone 38->39 tick is a step, not a cliff (spans %.2f of the height)" % rise)

# Past the floor, real ranges scale end to end as normal.
check(abs(sn["wide"][0]) < 1e-9 and abs(sn["wide"][-1] - 1.0) < 1e-9,
      "a 30-point range uses the full height (got %r)" % sn["wide"])
check(abs(sn["dip"][1] - sn["dip"][2]) > 0.1, "and a dip in the middle stays visible")
check(all(0.0 <= v <= 1.0 for k in sn for v in sn[k]), "nothing ever escapes 0..1")
check(sn["single"] == [0.5], "a lone sample sits mid-height rather than dividing by zero")
print("sparkline: flat sits at %.2f, one tick spans %.2f, a wide range uses the full 0..1"
      % (sn["flat"][0], rise))


# -- 10. a window that ended while we could not ask -----------------------------------------------
# The failure that stops us asking - a token that expires while the machine sleeps - is exactly the
# one that lasts long enough for a five-hour window to roll over underneath it. Holding up last
# night's number is not "old data", it is a specific wrong number, and greying it only claims the
# first. When the reset time has passed and nothing is running, the truth is zero and we can say so.
tmp2 = tempfile.mkdtemp(prefix="hud-roll-")
_sv = (hu.CACHE, hu.fetch)
hu.CACHE = os.path.join(tmp2, "usage.json")
hu.fetch = lambda timeout=10: None                    # the endpoint is unreachable throughout

past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
soon = (datetime.now(timezone.utc) + timedelta(minutes=45)).isoformat()
weekpast = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()


def _seed(resets, weekly_resets=None, **extra):
    d = {"ts": int(time.time() * 1000) - 20 * 60 * 1000, "session_pct": 40, "session_util": 40.0,
         "session_resets": resets, "weekly_pct": 12,
         "weekly_resets": weekly_resets or (datetime.now(timezone.utc) + timedelta(days=5)).isoformat(),
         "history": [[1, 40.0], [2, 40.0]], "burn": 0.17, "pace": 0.4, "projected": 44}
    d.update(extra)
    hu._publish(d)


# Still inside the window: an old reading is just old, and must stay old.
_seed(soon)
hu.refresh()
c = hu.read()
check(c["session_pct"] == 40, "a stale reading inside its window is left alone (got %r)" % c["session_pct"])
check(not c.get("inferred"), "and is not dressed up as knowledge")

# Window ended, nothing running: zero, and say so.
_seed(past)
hu.refresh(active=False)
c = hu.read()
check(c["session_pct"] == 0, "a window past its reset reads zero (got %r)" % c["session_pct"])
check(c.get("inferred") is True, "marked as worked out rather than measured")
check(c.get("session_resets") is None,
      "with no reset time invented - a fresh window has no anchor until you send something")
check(c.get("history") == [], "the old window's samples go with it")
check(c.get("burn") is None and c.get("pace") == 0.0, "and it is not burning anything")
check(abs(c["ts"] - time.time() * 1000) < 5000,
      "the timestamp moves, because this is current knowledge and should not read as stale")

# Window ended but a chat is mid-turn: we are being spent right now and do not know how much.
_seed(past)
hu.refresh(active=True)
c = hu.read()
check(c["session_pct"] == 40, "with work in flight we do not claim zero (got %r)" % c["session_pct"])
check(not c.get("inferred"), "and do not pretend to know")

# The weekly window rolls far less often, but the same rule applies when it does.
_seed(past, weekly_resets=weekpast)
hu.refresh(active=False)
c = hu.read()
check(c["weekly_pct"] == 0, "a passed weekly reset zeroes too (got %r)" % c["weekly_pct"])
_seed(past)
hu.refresh(active=False)
check(hu.read()["weekly_pct"] == 12, "but a weekly window still running is untouched")

# And a real reading always wins: `inferred` is not sticky.
_seed(past)
hu.refresh(active=False)
check(hu.read().get("inferred") is True, "inferred while the endpoint is down")
hu.fetch = lambda timeout=10: {"ts": int(time.time() * 1000), "session_pct": 5, "session_util": 5.0,
                               "session_resets": soon, "weekly_pct": 13, "weekly_resets": soon,
                               "severity": "normal"}
hu.refresh(force=True)
c = hu.read()
check(c["session_pct"] == 5 and not c.get("inferred"),
      "and the moment the endpoint answers, the guess is gone (got %r/%r)"
      % (c["session_pct"], c.get("inferred")))
print("rollover: a spent window reads zero, unless something is running, and never outranks a fetch")


# -- 11. an expired token is not a rate limit -------------------------------------------------------
# They fail the same way and want opposite treatment. A 429 is asking to be left alone for minutes;
# an expired token is fixed the instant you use Claude again, and sitting out ten minutes after that
# leaves the meter stale for no reason at all. This is what kept it grey for most of a night.
def _fail_with(code):
    err = Exception("boom")
    err.code = code

    def f(timeout=10):
        raise_it = err
        try:
            raise raise_it
        except Exception as e:
            hu.globals_err = None
        return None
    return f


for code, kind, ceiling in ((401, "auth", hu.AUTH_MAX_MS), (429, "rate", hu.FAIL_MAX_MS)):
    waits = []
    for n in range(4, 8):                       # deep into the backoff, where the ceiling bites
        hu._publish({"fail_n": n, "session_resets": soon})
        hu.LAST_ERR = kind                      # what fetch() would have recorded
        hu.fetch = lambda timeout=10: None
        t0 = time.time() * 1000
        hu.refresh()
        waits.append(int(round((hu.read()["next_try"] - t0) / 1000.0)))
    check(max(waits) <= ceiling / 1000 + 1,
          "%s failures cap at %ds (saw %r)" % (kind, ceiling / 1000, waits))
check(hu.AUTH_MAX_MS < hu.FAIL_MAX_MS,
      "and an expired token is retried sooner than a rate limit (%d vs %d)"
      % (hu.AUTH_MAX_MS, hu.FAIL_MAX_MS))
print("backoff: auth caps at %ds, rate limits at %ds" % (hu.AUTH_MAX_MS / 1000, hu.FAIL_MAX_MS / 1000))

hu.CACHE, hu.fetch = _sv
shutil.rmtree(tmp2, ignore_errors=True)

shutil.rmtree(tmp, ignore_errors=True)
print("\nOK - pace maths, backoff and the shipped colour ramp all hold")
