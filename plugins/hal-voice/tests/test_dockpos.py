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
print("flip: stands for %d..%d, hangs for %d..%d" % (0, N // 2 - 1, N // 2, N - 1))


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
check(int(st["up"]) == 800 - 2 * 36 - 28, "standing, the third tab is two rows above the anchor")
check(int(st["down"]) == 100 + 2 * 36, "hanging, it is two rows below it")
check(int(st["upfirst"]) == 800 - 28, "the first tab sits at the anchor when standing")
check(int(st["downfirst"]) == 100, "and at the anchor when hanging - same order, other direction")
print("stack: grows away from the anchor either way, order unchanged by the flip")


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
check(re.search(r"if \(Dock-Flipped\) \{ \$script:targetOff = \[int\]\(\$stack", METER),
      "the meter goes below the tabs when the dock hangs")
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


# -- 5b. the travel itself, driven through the real function -----------------------------------------
# Deliberately NOT recomputing the curve here. A test that reimplements the thing it is testing
# passes against its own copy and says nothing about the code that ships - so this sets the state
# Dock-AnchorY reads and asks IT, at elapsed times chosen by moving the start instant into the past.
#
# Three detents of travel (~266px over 220ms) keeps a few ms of scheduling jitter down to a couple
# of pixels, well inside the tolerances below.
def _travel(elapsed_ms, frm=None, to=3):
    """Ask a fresh process where the dock is, `elapsed_ms` into a move from detent 0 to `to`."""
    body = "\n".join([
        '$script:dockPosChecked = [int64]::MaxValue',   # freeze the cache: we are setting state by hand
        '$script:dockPos = %d' % to,
        '$script:dockFromY = [double]%s' % ("(Dock-DetentY 0)" if frm is None else str(frm)),
        '$script:dockStartMs = (NowMs) - %d' % elapsed_ms,
        'Write-Output ("v|{0}" -f (Dock-AnchorY))',
        'Write-Output ("from|{0}" -f [int]$script:dockFromY)',
        'Write-Output ("to|{0}" -f (Dock-DetentY %d))' % to,
        'Write-Output ("dur|{0}" -f $script:DOCK_MOVE_MS)',
    ])
    return _ps(body)


DUR = int(_travel(0)["dur"])
at0 = _travel(0)
FROM, TO = int(at0["from"]), int(at0["to"])
span = FROM - TO
check(span > 200, "three detents is a decent span to measure over (%dpx)" % span)

check(abs(int(at0["v"]) - FROM) <= 8, "at the start it is where it set off from (got %s)" % at0["v"])
done = _travel(DUR + 200)
check(int(done["v"]) == TO, "once the move is over it sits exactly on the detent (got %s)" % done["v"])
late = _travel(DUR * 4)
check(int(late["v"]) == TO, "and stays there rather than drifting past (got %s)" % late["v"])

# Smoothstep, not a ramp - asserted by SHAPE rather than by absolute value at a chosen instant.
# Setting up the state costs a few unpredictable milliseconds before the reading is taken, which
# shifts every sample by the same unknown amount; the shape survives that, a single absolute value
# does not. (Learned the hard way: the first version of this compared one sample against a computed
# value and failed against correct code.)
covered = [FROM - int(_travel(int(DUR * f))["v"]) for f in (0.10, 0.35, 0.65, 0.90)]
check(covered == sorted(covered), "the travel only moves one way: %r" % covered)
slices = [covered[i + 1] - covered[i] for i in range(3)]
check(slices[1] > slices[0] * 1.3,
      "it eases IN - the middle of the move covers more than the start (%r of %dpx)" % (slices, span))
check(slices[1] > slices[2] * 1.3,
      "and eases OUT - and more than the end (%r)" % slices)

# Two processes, same instant, same answer - the property the whole design rests on.
a, b = _travel(int(DUR * 0.4)), _travel(int(DUR * 0.4))
check(abs(int(a["v"]) - int(b["v"])) <= 8,
      "two processes agree mid-travel (%s vs %s)" % (a["v"], b["v"]))
print("travel: real function, eased, settles exactly, agrees across processes")


# A move started mid-flight sets off from where the dock VISUALLY is, not from the detent it was
# last heading for. Without that, dragging quickly through several detents restarts from a stale
# point each time and the dock stutters backwards.
mv = _ps("\n".join([
    '$script:DockPosFile = "%s"' % os.path.join(tmp, "pos3").replace("\\", "\\\\"),
    '$script:dockPosChecked = [int64]::MaxValue',
    '$script:dockPos = 6',
    '$script:dockFromY = [double](Dock-DetentY 0)',
    '$script:dockStartMs = (NowMs) - %d' % (int(DUR * 0.5)),
    'Write-Output ("mid|{0}" -f (Dock-AnchorY))',
    '[void](Set-DockPos 9)',                         # change target while still travelling
    'Write-Output ("newfrom|{0}" -f [int]$script:dockFromY)',
    'Write-Output ("d0|{0}" -f (Dock-DetentY 0))',
    'Write-Output ("d6|{0}" -f (Dock-DetentY 6))',
]))
midv, newfrom = int(mv["mid"]), int(mv["newfrom"])
d0, d6 = int(mv["d0"]), int(mv["d6"])
# Not a tight tolerance against `midv`: setting up and reading costs milliseconds during which the
# dock genuinely travels on, so the two are not the same instant. What must be true is that the new
# move set off from a point ON the travel - never from the detent it started at, and never from the
# one it was heading for, either of which is the stale-restart bug.
check(newfrom <= midv,
      "a move begun mid-flight sets off from at least as far along as it had got (%d vs %d)"
      % (newfrom, midv))
check(newfrom < d0 - 20 and newfrom > d6 + 20,
      "and from between the two detents rather than either of them (%d, between %d and %d)"
      % (newfrom, d6, d0))
print("restart: a new target mid-drag continues from the current position")


# -- 6. a press is a click or a drag, and only one of them stows --------------------------------------
check("$script:DRAG_SLOP" in DOCK, "the handle has a slop threshold")
check(re.search(r"if \(-not \$script:dragging\) \{ Set-DockStowed", DOCK),
      "stowing happens on release, and only if it never became a drag")
check(re.search(r"\$want = Dock-PosFor", DOCK), "a drag snaps to detents rather than moving freely")
check(not re.search(r"if \(-not \(InStrip\)\) \{ return \}\s*\n\s*Set-DockStowed", DOCK),
      "and the press itself no longer stows, or a drag would toggle on the way out")
print("handle: press is ambiguous until release; drag moves, click stows")

import shutil                            # noqa: E402
shutil.rmtree(tmp, ignore_errors=True)
print("\nOK - ten notches up the side, and the dock turns over at halfway")
