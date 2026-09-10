"""Dragging the dock up the side of the screen, and the flip that comes with it.

Ten detents rather than free movement: at a hundred possible positions you spend effort placing it,
and the whole point of the dock is that you stop thinking about it.

The flip is the part that has to be right. The stack grows AWAY from its anchor, so a dock dragged
near the top would grow straight off the screen unless it turns over - hanging from the anchor
downward, with the meter still on the far side of the tabs. Every overlay derives its own position
from the same two functions so they cannot disagree about where the dock is or which way up it is.

Drives the real functions out of popup_common.ps1.
"""
import os, re, subprocess, sys, tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import check              # noqa: E402

SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
COMMON_PS1 = os.path.join(SCRIPTS, "popup_common.ps1")
COMMON = open(COMMON_PS1, encoding="utf-8").read()
BADGE = open(os.path.join(SCRIPTS, "badge.ps1"), encoding="utf-8").read()
METER = open(os.path.join(SCRIPTS, "hal_meter.ps1"), encoding="utf-8").read()
DOCK = open(os.path.join(SCRIPTS, "hal_dock.ps1"), encoding="utf-8").read()
tmp = tempfile.mkdtemp(prefix="hud-pos-")


def _ps(body):
    p = os.path.join(tmp, "probe.ps1")
    with open(p, "w", encoding="utf-8") as f:
        f.write('. "%s"\n' % COMMON_PS1.replace("\\", "\\\\"))
        f.write('$script:DockPosFile = "%s"\n' % os.path.join(tmp, "pos").replace("\\", "\\\\"))
        f.write(body)
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-File", p],
                       capture_output=True, text=True)
    check(r.returncode == 0, "the probe runs: %s" % (r.stderr or "")[:400])
    return dict(l.split("|", 1) for l in r.stdout.strip().splitlines() if "|" in l)



METER_PS1 = os.path.join(SCRIPTS, "hal_meter.ps1")
PGLOW = int(re.search(r"\$PGLOW = (\d+)", METER).group(1))


def _psm(body):
    """Like _ps, but with hal_meter's placement helpers available too. They are pulled out by name
    rather than dot-sourcing the whole overlay, which would try to open a window."""
    src = "\n".join(_fn_from(METER, n) for n in ("Meter-OffsetFor", "Panel-TopFor"))
    consts = "\n".join("$%s = %s" % (k, v) for k, v in (
        ("GLOW", re.search(r"^\$GLOW = (\d+)", METER, re.M).group(1)),
        ("PGLOW", str(PGLOW)),
        ("CONTENT_H", str(CONTENT_H)),
    ))
    return _ps(consts + "\n" + src + "\n" + body)


def _fn_from(src, name):
    i = src.index("function %s" % name)
    depth, j = 0, src.index("{", i)
    for k in range(j, len(src)):
        if src[k] == "{":
            depth += 1
        elif src[k] == "}":
            depth -= 1
            if depth == 0:
                return src[i:k + 1]
    raise AssertionError("unbalanced braces reading %s" % name)


got = _ps("""
Write-Output ('n|{0}' -f $script:DOCK_DETENTS)
Write-Output ('step|{0:F4}' -f (Dock-Step))
$wa = [System.Windows.Forms.Screen]::PrimaryScreen.WorkingArea
Write-Output ('bottom|{0}' -f $wa.Bottom)
Write-Output ('mb|{0}' -f $script:DOCK_MARGIN_B)
Write-Output ('mt|{0}' -f $script:DOCK_MARGIN_T)
foreach ($p in 0..($script:DOCK_DETENTS - 1)) {
    Write-Output ('a{0}|{1}' -f $p, (Dock-AnchorY $p))
    Write-Output ('f{0}|{1}' -f $p, (Dock-Flipped $p))
}
# clamping, and the round trip through the snap
foreach ($p in -3, 0, 5, 99) { Write-Output ('c{0}|{1}' -f $p, (Set-DockPos $p)) }
foreach ($p in 0..($script:DOCK_DETENTS - 1)) {
    Write-Output ('r{0}|{1}' -f $p, (Dock-PosFor (Dock-AnchorY $p)))
}
Write-Output ('low|{0}' -f (Dock-PosFor 99999))
Write-Output ('high|{0}' -f (Dock-PosFor -99999))
""")

N = int(got["n"])
STEP = float(got["step"])
BOTTOM, MB, MT = int(got["bottom"]), int(got["mb"]), int(got["mt"])
anchors = [int(got["a%d" % p]) for p in range(N)]
flips = [got["f%d" % p] == "True" for p in range(N)]

# -- 1. ten detents spanning the usable height --------------------------------------------------
check(N == 10, "ten positions, as asked (got %d)" % N)
check(anchors[0] == BOTTOM - MB, "the lowest sits where the dock has always sat (%d)" % anchors[0])
check(abs(anchors[-1] - MT) <= 1, "and the highest reaches the top margin (%d, want %d)" % (anchors[-1], MT))
check(anchors == sorted(anchors, reverse=True), "they climb the screen in order: %r" % anchors)
gaps = [anchors[i] - anchors[i + 1] for i in range(N - 1)]
check(max(gaps) - min(gaps) <= 1, "evenly spaced, so each drag step feels the same (%r)" % gaps)
check(abs(STEP - (BOTTOM - MB - MT) / (N - 1.0)) < 0.01, "the step divides the travel exactly")
print("detents: %d positions, %.0fpx apart, %d down to %d" % (N, STEP, anchors[0], anchors[-1]))


# -- 2. the flip, and where it happens -------------------------------------------------------------
check(flips == [False] * (N // 2) + [True] * (N - N // 2),
      "the bottom half stands, the top half hangs: %r" % flips)
check(not flips[N // 2 - 1] and flips[N // 2], "and it turns over exactly at halfway")
check(anchors[N // 2] < (BOTTOM - MB + MT) / 2 + STEP,
      "which is also physically past the middle of the screen")
# And with no argument it follows where the dock IS, not the detent it is heading for. Off the
# target, the layout inverts the instant you release the handle and the dock then slides to meet it -
# a jump followed by a move. Off the live position it turns over as it passes the middle, so the
# whole thing reads as one motion.
live = _ps("\n".join([
    '$script:dockPosChecked = [int64]::MaxValue',
    '$script:dockPos = %d' % (N - 1),                 # heading for the very top...
    '$script:dockFromY = [double](Dock-DetentY 0)',   # ...but still down at the bottom
    '$script:dockStartMs = NowMs',
    'Write-Output ("headingup|{0}" -f (Dock-Flipped))',
    '$script:dockPos = 0',                            # and now the reverse
    '$script:dockFromY = [double](Dock-DetentY %d)' % (N - 1),
    '$script:dockStartMs = NowMs',
    'Write-Output ("headingdown|{0}" -f (Dock-Flipped))',
]))
check(live["headingup"] == "False",
      "still standing while it is only on its way up (got %s)" % live["headingup"])
check(live["headingdown"] == "True",
      "and still hanging while it is only on its way down (got %s)" % live["headingdown"])
print("flip: stands for %d..%d, hangs for %d..%d, and turns over on arrival not on release"
      % (0, N // 2 - 1, N // 2, N - 1))


# -- 3. dragging snaps back to the detent it came from ----------------------------------------------
for p in range(N):
    check(int(got["r%d" % p]) == p, "an anchor snaps back to its own detent (%d -> %s)" % (p, got["r%d" % p]))
check(int(got["low"]) == 0 and int(got["high"]) == N - 1, "and a drag past either end clamps")
for asked, want in (("-3", 0), ("0", 0), ("5", 5), ("99", N - 1)):
    check(int(got["c" + asked]) == want, "Set-DockPos(%s) clamps to %d" % (asked, want))

# Halfway between two detents must land on one of them, never between.
half = int(STEP / 2)
mid = _ps("\n".join(
    "Write-Output ('m%d|{0}' -f (Dock-PosFor ((Dock-AnchorY %d) - %d)))" % (p, p, half)
    for p in range(N - 1)))
for p in range(N - 1):
    check(int(mid["m%d" % p]) in (p, p + 1), "a half-step lands on a detent (%d -> %s)" % (p, mid["m%d" % p]))
print("snapping: every anchor round-trips, ends clamp, half-steps land on a notch")


# -- 4. the stack grows away from the anchor, whichever way up it is ---------------------------------
# This is the property that makes the flip mean anything: unflipped the tabs go up from the anchor,
# flipped they come down from it, and in both cases the first tab is the one nearest the anchor - so
# turning the dock over does not reshuffle the order, it only reverses the direction.
st = _ps("""
$script:PopupId = 'me'
$ordered = @([pscustomobject]@{id='a';h=28}, [pscustomobject]@{id='b';h=28}, [pscustomobject]@{id='me';h=28})
Write-Output ('up|{0}'   -f (Stack-TargetBottom 800 8 $ordered 28 $false))
Write-Output ('down|{0}' -f (Stack-TargetBottom 100 8 $ordered 28 $true))
$first = @([pscustomobject]@{id='me';h=28}, [pscustomobject]@{id='b';h=28})
Write-Output ('upfirst|{0}'   -f (Stack-TargetBottom 800 8 $first 28 $false))
Write-Output ('downfirst|{0}' -f (Stack-TargetBottom 100 8 $first 28 $true))
""")
check(int(st["up"]) == 800 - 2 * 36 - 28, "standing, the last of three is two rows above the anchor")
check(int(st["down"]) == 100, "and hanging, that same tab is AT the anchor - it is still the top one")
check(int(st["upfirst"]) == 800 - 28, "the first tab sits at the anchor when standing - the bottom")
check(int(st["downfirst"]) == 100 + 36, "and one row down from it when hanging - still the lower")
print("stack: grows away from the anchor either way")


# The order you SEE must not change. Nearest-the-anchor is bottom-most standing and top-most
# hanging, so measuring from the same end of the list turns the column upside down at the midpoint -
# which is exactly what it used to do. Reported as "don't make the order of the tabs change when I
# go up"; the only thing that should move is the meter.
def _visual_order(flipped):
    ids = ["t0", "t1", "t2", "t3"]
    ordered = ", ".join("[pscustomobject]@{id='%s';h=28}" % i for i in ids)
    body = ["$ordered = @(%s)" % ordered]
    for i in ids:
        body.append("$script:PopupId = '%s'" % i)
        body.append("Write-Output ('%s|{0}' -f (Stack-TargetBottom %d 8 $ordered 28 $%s))"
                    % (i, 100 if flipped else 800, "true" if flipped else "false"))
    got = _ps("\n".join(body))
    return [i for i in sorted(ids, key=lambda k: int(got[k]))]


up_order, down_order = _visual_order(False), _visual_order(True)
check(up_order == down_order,
      "top to bottom, the tabs read the same standing and hanging (%r vs %r)" % (up_order, down_order))
check(len(set(up_order)) == 4, "and every tab is somewhere (%r)" % up_order)
print("order: identical top-to-bottom either way - %s" % " -> ".join(up_order))


# -- 5. everyone derives it, nobody keeps a copy -----------------------------------------------------
# Three separate processes have to agree on where the dock is and which way up. The moment one of
# them caches its own answer at startup they disagree, and the dock tears apart as it moves.
check("Dock-AnchorY" in BADGE and "Dock-Flipped" in BADGE, "the tabs read the shared anchor")
check("Dock-AnchorY" in METER and "Dock-Flipped" in METER, "so does the meter")
check("Dock-AnchorY" in DOCK, "and so does the handle")
check(re.search(r"\$script:bottomAnchor = \(Dock-AnchorY\)", BADGE),
      "a tab recomputes its anchor rather than fixing it at startup")
check(re.search(r"\$script:targetOff = Stack-TargetBottom", BADGE),
      "and passes the flip through to the geometry")
check(re.search(r"\$script:targetOff = Meter-OffsetFor \$stack \(Dock-Flipped\)", METER),
      "the meter takes its offset from the shared rule rather than inlining it")
check(not re.search(r"\$dockBottom = \$screen\.Bottom - 44", METER),
      "and no fixed bottom survives in the meter")

# THE property, and the one that was wrong: the anchor is taken RAW every frame while only the
# offset within the stack is eased. Easing the anchor as well - which is what happens if you treat
# it as just another target - makes every window lag the drag by its own spring, at its own poll
# rate, and the dock comes apart while it moves.
for name, src in (("a tab", BADGE), ("the meter", METER)):
    check(re.search(r"\$script:curTop = \(Dock-AnchorY\) \+ \$script:curOff", src),
          "%s draws at anchor + eased offset, not at an eased absolute position" % name)
    check(re.search(r"\$script:cur(Off|Top) = \$script:targetOff \} else \{ \$script:curOff \+=", src)
          or re.search(r"\$script:curOff \+= \$delta", src),
          "%s eases the OFFSET" % name)
check(not re.search(r"\$script:curTop \+= \$delta", BADGE + METER),
      "and neither of them eases an absolute y any more")
check("$script:targetOff" in BADGE and "$script:targetOff" in METER, "both speak in offsets")
print("wiring: three processes, one anchor, one flip, nobody eases the anchor")


# -- 5b. the travel itself -----------------------------------------------------------------------
# Driven through the SHIPPED curve at instants we choose. Two earlier versions of this section were
# wrong in opposite directions and both are worth remembering: the first recomputed the smoothstep
# in the test and compared two copies of my own arithmetic, so mutating the real function changed
# nothing and three mutants walked through it. The second asked two processes "where is it now",
# which measures the scheduler rather than the code - it failed roughly one run in three, against
# correct code. Passing the clock IN is what makes it both real and repeatable.
FROM, TO = 800, 200
SPAN = FROM - TO
curve = _ps("\n".join(
    ['Write-Output ("dur|{0}" -f $script:DOCK_MOVE_MS)'] +
    ['Write-Output ("t%d|{0}" -f (Dock-Travel %d %d %%d))'.replace("%%d", str(ms)) % (ms, FROM, TO)
     for ms in (0, 20, 55, 110, 165, 200, 220, 500)] +
    # Parenthesised: PowerShell reads a bare -50 in argument position as a parameter name.
    ['Write-Output ("neg|{0}" -f (Dock-Travel %d %d (-50)))' % (FROM, TO)]))
DUR = int(curve["dur"])
at = dict((ms, int(curve["t%d" % ms])) for ms in (0, 20, 55, 110, 165, 200, 220, 500))

check(at[0] == FROM, "at zero it is where it set off from (got %d)" % at[0])
check(int(curve["neg"]) == FROM, "and before that too, rather than overshooting backwards")
check(at[220] == TO and at[500] == TO,
      "once the move is over it sits exactly on the target and stays (%d, %d)" % (at[220], at[500]))
check(at[110] == FROM - SPAN // 2, "halfway through the time is halfway through the distance (%d)" % at[110])

vals = [at[ms] for ms in (0, 20, 55, 110, 165, 200, 220)]
check(vals == sorted(vals, reverse=True), "the travel only moves one way: %r" % vals)

# Smoothstep, not a ramp: it leaves and arrives slowly. Exact numbers, because the clock is an
# argument now - no tolerance needed and nothing to be flaky about.
first, mid, last = FROM - at[55], at[55] - at[165], at[165] - TO
check(mid > first * 1.5, "it eases in - the middle covers far more than the start (%d vs %d)" % (mid, first))
check(mid > last * 1.5, "and eases out - far more than the end (%d vs %d)" % (mid, last))
check(first == last, "symmetrically (%d vs %d)" % (first, last))
check(at[20] > FROM - SPAN * 0.05, "barely moves in the first tenth (%d of %d)" % (FROM - at[20], SPAN))

# Two processes, identical inputs, identical output - the property the whole design rests on, now
# asked in a way that can actually answer it.
again = _ps("\n".join(
    'Write-Output ("t%d|{0}" -f (Dock-Travel %d %d %s))' % (ms, FROM, TO, ms)
    for ms in (0, 20, 55, 110, 165, 200, 220, 500)))
check(all(again["t%d" % ms] == curve["t%d" % ms] for ms in at),
      "two processes agree exactly at every instant: %r vs %r"
      % ([again["t%d" % m] for m in sorted(at)], [curve["t%d" % m] for m in sorted(at)]))
print("travel: shipped curve, exact, symmetric, identical across processes")


# The live function has to actually use it, and settle on the detent when the move is done. This is
# the only part that reads the clock, so it is asserted loosely on purpose.
settled = _ps("\n".join([
    '$script:dockPosChecked = [int64]::MaxValue',
    '$script:dockPos = 3',
    '$script:dockFromY = [double](Dock-DetentY 0)',
    '$script:dockStartMs = (NowMs) - ($script:DOCK_MOVE_MS * 3)',
    'Write-Output ("v|{0}" -f (Dock-AnchorY))',
    'Write-Output ("d3|{0}" -f (Dock-DetentY 3))',
    'Write-Output ("moving|{0}" -f (Dock-PosMoving))',
]))
check(int(settled["v"]) == int(settled["d3"]),
      "a finished move leaves the dock exactly on its detent (%s vs %s)" % (settled["v"], settled["d3"]))
check(settled["moving"] == "False", "and it stops reporting itself as moving")
check("Dock-Travel" in COMMON and "return Dock-Travel" in COMMON,
      "and Dock-AnchorY goes through the same curve rather than its own copy")


# A move begun mid-flight sets off from where the dock VISUALLY is, never from the detent it was
# heading for - otherwise dragging quickly through several notches restarts from a stale point and
# the dock stutters backwards.
mv = _ps("\n".join([
    '$script:DockPosFile = "%s"' % os.path.join(tmp, "pos3").replace("\\", "\\\\"),
    '$script:dockPosChecked = [int64]::MaxValue',
    '$script:dockPos = 6',
    '$script:dockFromY = [double](Dock-DetentY 0)',
    # A third of the way in, so the move is still comfortably in flight even if this
    # process is descheduled for a hundred milliseconds between here and the next line.
    '$script:dockStartMs = (NowMs) - 70',
    '[void](Set-DockPos 9)',
    'Write-Output ("newfrom|{0}" -f [int]$script:dockFromY)',
    'Write-Output ("d0|{0}" -f (Dock-DetentY 0))',
    'Write-Output ("d6|{0}" -f (Dock-DetentY 6))',
]))
newfrom, d0, d6 = int(mv["newfrom"]), int(mv["d0"]), int(mv["d6"])
check(newfrom < d0 - 10 and newfrom > d6 + 10,
      "a move begun mid-flight sets off from between the detents, not from either of them "
      "(%d, between %d and %d)" % (newfrom, d6, d0))
print("restart: a new target mid-drag continues from the current position")


# -- 6. a press is a click or a drag, and only one of them stows --------------------------------------
check("$script:DRAG_SLOP" in DOCK, "the handle has a slop threshold")
check(re.search(r"if \(-not \$script:dragging\) \{ Set-DockStowed", DOCK),
      "stowing happens on release, and only if it never became a drag")
check(re.search(r"\$want = Dock-PosFor", DOCK), "a drag snaps to detents rather than moving freely")
check(not re.search(r"if \(-not \(InStrip\)\) \{ return \}\s*\n\s*Set-DockStowed", DOCK),
      "and the press itself no longer stows, or a drag would toggle on the way out")
print("handle: press is ambiguous until release; drag moves, click stows")


# -- 7. nothing in the dock may overlap anything else ------------------------------------------------
# The bug this exists for: hanging, the tabs sat 2*GLOW too low and the meter - correctly placed a
# gap below the last tab - was drawn straight through them. The detail panel did the same, because
# it always opened upward, which is over the stack once the meter has moved to the bottom.
#
# Both were geometry, and geometry is exactly what nobody notices until they look at the screen. So
# this lays the whole dock out through the shipped functions and asserts the rectangles are disjoint.
GLOW_B = int(re.search(r"\$GLOW=(\d+)", BADGE).group(1))
CH = int(re.search(r"\$script:CH = (\d+)", BADGE).group(1))
GAP = int(re.search(r"^\$GAP = (\d+)", BADGE, re.M).group(1))
GLOW_M = int(re.search(r"^\$GLOW = (\d+)", METER, re.M).group(1))
CONTENT_H = int(re.search(r"\$UPCT_H \+ 2 \+ \$SBAR_H \+ \$GAP_BARS \+ \$WBAR_H", METER) and
                _ps('Write-Output ("h|{0}" -f (%d + 2 + %d + %d + %d))' % (
                    int(re.search(r"\$UPCT_H = (\d+)", METER).group(1)),
                    int(re.search(r"\$SBAR_H = (\d+)", METER).group(1)),
                    int(re.search(r"\$GAP_BARS = (\d+)", METER).group(1)),
                    int(re.search(r"\$WBAR_H = (\d+)", METER).group(1))))["h"])
GAPB = int(re.search(r"^\$GAPB = (\d+)", METER, re.M).group(1))

# The glow term badge.ps1 actually passes when it places a tab. Lifted from the source so that
# changing it in the product changes it here too - which is the whole point of the check below.
_m = re.search(r"Stack-TargetBottom (.+?) \$GAP \$ordered", BADGE)
check(_m is not None, "found badge.ps1's own call to Stack-TargetBottom")
GLOW_ARG = _m.group(1).strip()


def _layout(n_tabs, flipped):
    """Screen rects for every tab chip, the meter's content, and the panel - all from real code."""
    ids = ["t%d" % i for i in range(n_tabs)]
    ordered = ", ".join("[pscustomobject]@{id='%s';h=%d}" % (i, CH) for i in ids)
    stack = n_tabs * CH + (n_tabs - 1) * GAPB + GAPB
    body = ["$ordered = @(%s)" % ordered,
            "$GLOW = %d" % GLOW_B,
            "$script:flipped = $%s" % ("true" if flipped else "false"),
            "$anchor = Dock-AnchorY %d" % (7 if flipped else 0),
            "Write-Output ('anchor|{0}' -f $anchor)"]
    for i in ids:
        body.append("$script:PopupId = '%s'" % i)
        # GLOW_ARG is lifted verbatim from badge.ps1's own call, not written here. Passing -$GLOW
        # myself is what let the reported bug survive this test: the offset was right because I
        # supplied the right argument, while the shipped code supplied the wrong one.
        body.append("Write-Output ('tab%s|{0}' -f (Stack-TargetBottom %s %d $ordered %d $script:flipped))"
                    % (i, GLOW_ARG, GAP, CH))
    got = _ps("\n".join(body))
    anchor = int(got["anchor"])
    # form top -> chip rect on screen
    tabs = [(anchor + int(got["tab" + i]) + GLOW_B,
             anchor + int(got["tab" + i]) + GLOW_B + CH) for i in ids]

    m = _psm("Write-Output ('off|{0}' -f (Meter-OffsetFor %d $%s))"
             % (stack, "true" if flipped else "false"))
    moff = int(m["off"])
    meter_form_top = anchor + moff
    meter = (meter_form_top + GLOW_M, meter_form_top + GLOW_M + CONTENT_H)

    pnl = _psm("Write-Output ('t|{0}' -f (Panel-TopFor %d %d $%s %d))"
               % (meter_form_top, 400, "true" if flipped else "false", 885))
    ptop = int(pnl["t"])
    panel = (ptop + PGLOW, ptop + PGLOW + 400)
    return tabs, meter, panel


def _overlap(a, b):
    return min(a[1], b[1]) - max(a[0], b[0])


for flipped in (False, True):
    for n in (1, 3, 6):
        tabs, meter, panel = _layout(n, flipped)
        which = "hanging" if flipped else "standing"
        for i in range(len(tabs) - 1):
            ov = _overlap(tabs[i], tabs[i + 1])
            check(ov <= 0, "%s, %d tabs: tab %d and %d overlap by %d" % (which, n, i, i + 1, ov))
            check(abs(abs(ov) - GAPB) <= 1,
                  "%s: consecutive tabs are exactly one gap apart (%d, want %d)" % (which, -ov, GAPB))
        for i, t in enumerate(tabs):
            ov = _overlap(t, meter)
            check(ov <= 0, "%s, %d tabs: the meter overlaps tab %d by %dpx  tabs=%r meter=%r"
                           % (which, n, i, ov, tabs, meter))
            ov = _overlap(t, panel)
            check(ov <= 0, "%s, %d tabs: the panel overlaps tab %d by %dpx  tab=%r panel=%r"
                           % (which, n, i, ov, t, panel))
        # Measured against the extreme tab rather than a position in the list: which list index is
        # visually last is exactly what changed here, so indexing would bake the old order into the
        # test. The property is geometric - the meter is one gap beyond the end of the column.
        lowest, highest = max(t[1] for t in tabs), min(t[0] for t in tabs)
        gap_to_meter = (meter[0] - lowest) if flipped else (highest - meter[1])
        check(abs(gap_to_meter - GAPB) <= 1,
              "%s: the meter sits exactly one gap beyond the end of the stack (%d, want %d)"
              % (which, gap_to_meter, GAPB))
        check(_overlap(meter, panel) <= 0,
              "%s: the panel does not cover the meter that opened it (%r vs %r)" % (which, meter, panel))
print("layout: tabs, meter and panel are disjoint standing and hanging, 1..6 tabs")


import shutil                            # noqa: E402
shutil.rmtree(tmp, ignore_errors=True)
print("\nOK - ten notches up the side, and the dock turns over at halfway")
