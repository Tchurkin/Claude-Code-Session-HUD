"""The chart's resolution: can a question you just asked actually show up?

The plan's utilization cannot answer that. It arrives in whole percentage points, no oftener than
every 45 seconds, so a single turn is invisible until it happens to tip a point over - minutes of
latency, quantized into a staircase. Every assistant call is already on disk with its real timestamp
and its exact weight, and the tailer sees it within five seconds, so that is what the chart draws.

This pins the two properties that trade against each other: fine enough to resolve one turn, smooth
enough to read as a line rather than a comb.
"""
import os, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import check              # noqa: E402

import hal_tokens as ht                 # noqa: E402

NOW = 1_700_000_000_000
BUCKET = ht.CHART_BUCKET


def _at(seconds_ago, weight, sid="aaaa1111"):
    return [NOW - int(seconds_ago * 1000), float(weight), 0, 0, sid]


# -- 1. resolution ---------------------------------------------------------------------------------
check(BUCKET <= 10000, "buckets are seconds wide, not minutes (got %dms)" % BUCKET)
sr = ht.series([], NOW)
check(len(sr) == ht.CHART_MS // BUCKET, "a point per bucket across the window (got %d)" % len(sr))
check(all(v == 0 for _, v in sr), "and nothing happening reads as nothing")

span = sr[-1][0] - sr[0][0]
check(abs(span - (ht.CHART_MS - BUCKET)) < BUCKET, "the points span the window (%dms)" % span)
gaps = set(sr[i + 1][0] - sr[i][0] for i in range(len(sr) - 1))
check(gaps == {BUCKET}, "evenly spaced, so the line is not secretly stretched (%r)" % gaps)


# -- 2. one turn is visible, and lands where it happened ---------------------------------------------
# The whole point of the exercise. A single call thirty seconds ago has to move the line, near the
# right-hand end, having been invisible to the utilization series entirely.
one = ht.series([_at(30, 400_000)], NOW)
peak_i = max(range(len(one)), key=lambda i: one[i][1])
peak_age = (NOW - one[peak_i][0]) / 1000.0
check(one[peak_i][1] > 0, "a single turn moves the line at all")
check(abs(peak_age - 30) <= ht.CHART_HALF * BUCKET / 1000.0 + 3,
      "and peaks where it happened, ~30s ago (got %.0fs)" % peak_age)
check(one[-1][1] == 0 or one[-1][1] < one[peak_i][1],
      "with the line already coming back down after it")

# Two turns a minute apart stay two humps, rather than merging into one plateau.
two = [v for _, v in ht.series([_at(200, 300_000), _at(140, 300_000)], NOW)]
peaks = [i for i in range(1, len(two) - 1) if two[i] > two[i - 1] and two[i] >= two[i + 1] and two[i] > 0]
check(len(peaks) == 2, "two turns a minute apart read as two events (found %d)" % len(peaks))


# -- 3. smooth enough to read as a line --------------------------------------------------------------
# Unsmoothed five-second buckets are a row of spikes with gaps between them: accurate, and unreadable.
# A burst has to arrive and leave over several points rather than in one.
burst = [v for _, v in ht.series([_at(120, 500_000)], NOW)]
nonzero = [i for i, v in enumerate(burst) if v > 0]
check(len(nonzero) >= 3, "a single call spreads over several points (got %d)" % len(nonzero))
check(len(nonzero) <= 2 * ht.CHART_HALF + 3,
      "but not so many that it stops saying when (got %d)" % len(nonzero))
rise = [burst[i] for i in nonzero]
check(rise == sorted(rise[:len(rise) // 2 + 1]) + sorted(rise[len(rise) // 2 + 1:], reverse=True)
      or max(rise) == rise[len(rise) // 2],
      "and it rises to a peak and falls, rather than jumping (got %r)" % rise)


# -- 4. the arithmetic is a rate, not a total --------------------------------------------------------
# A bucket holds what landed in it; the rate is that over the bucket's length. Get this wrong and
# every number on the chart is out by a factor of twelve.
solo = ht.series([_at(100, 100_000)], NOW, half=0)     # no smoothing, so the peak is the bucket
top = max(v for _, v in solo)
expect = 100_000 / (BUCKET / 60000.0)
check(abs(top - expect) < 2, "one call of 100k in a %ds bucket is %s/min (got %s)"
                             % (BUCKET / 1000, "{:,.0f}".format(expect), "{:,}".format(top)))

# Smoothing must not invent or destroy spend: the area under the line is conserved.
raw = sum(v for _, v in ht.series([_at(150, 240_000), _at(90, 60_000)], NOW, half=0))
sm = sum(v for _, v in ht.series([_at(150, 240_000), _at(90, 60_000)], NOW))
check(abs(raw - sm) / max(raw, 1) < 0.02,
      "smoothing conserves what was spent (%s vs %s)" % ("{:,}".format(raw), "{:,}".format(sm)))


# -- 5. it does not read things it should not --------------------------------------------------------
check(all(v == 0 for _, v in ht.series([_at(60 * 60, 900_000)], NOW)),
      "an hour-old call is outside the window")
check(all(v == 0 for _, v in ht.series([_at(-120, 900_000)], NOW)),
      "and one stamped in the future is ignored rather than drawn off the end")
check(ht.series([[NOW, "junk"], None, "nope", [NOW - 5000, 1000.0, 0, 0, "x"]], NOW),
      "malformed entries are skipped rather than raising")


# -- 6. it beats the source it replaced ---------------------------------------------------------------
# Stated as a comparison because that is the actual requirement: the old series could not see this.
import hal_usage as hu                  # noqa: E402
LONG_MIN = 60000
flat_then_burst = [[NOW - (10 - i) * LONG_MIN, 40.0] for i in range(11)]
old = hu.rate_series(flat_then_burst)
check(all(v == 0.0 for _, v in old),
      "a turn that has not yet tipped a whole point is invisible to the utilization series")
check(max(v for _, v in ht.series([_at(20, 400_000)], NOW)) > 0,
      "while the token series shows it twenty seconds later")
check(BUCKET < hu.LONG_EVERY_MS,
      "and its buckets are finer than that series' whole sample interval (%ds vs %ds)"
      % (BUCKET / 1000, hu.LONG_EVERY_MS / 1000))
print("series: %d points at %ds, one turn resolved, area conserved, %dx finer than utilization"
      % (len(sr), BUCKET / 1000, hu.LONG_EVERY_MS / BUCKET))

meter = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "scripts", "hal_meter.ps1"), encoding="utf-8").read()
check("$h = @($script:series)" in meter, "the chart draws the token series")
check("$h = @($script:rates); $tokUnits = $false" in meter,
      "and falls back to the coarse one only when the tailer has nothing")
check("peak " in meter, "the axis says peak, since the line is instantaneous and TOKENS is an average")
print("wiring: chart on the tailer, utilization series kept as the fallback")

print("\nOK - the chart can see a single turn, and still reads as a line")
