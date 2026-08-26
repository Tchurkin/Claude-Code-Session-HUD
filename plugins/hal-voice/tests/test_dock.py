"""Sliding the whole dock off the right edge, as one object.

The dock is not one window. It is a tab per open chat plus the usage meter, each its own process
with its own frame clock, and the first version had each of them ease toward its own target. They
drifted apart immediately - and worse, each tab travelled a DIFFERENT distance, because a chip is
only as wide as its label, so the narrow ones arrived while the wide ones were still going. It read
as things scattering rather than as a panel closing.

So nothing eases anything now. The flag carries the instant the move begins and every overlay
evaluates the same curve against the same wall clock. This pins that: the curve, the fact that all
three consumers use it rather than rolling their own, and the geometry that lets the handle be
clicked at all.
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

tmp = tempfile.mkdtemp(prefix="hud-dock-")


def _ps(lines):
    p = os.path.join(tmp, "probe.ps1")
    with open(p, "w", encoding="utf-8") as f:
        f.write('. "%s"\n' % COMMON_PS1.replace("\\", "\\\\"))
        f.write("\n".join(lines) + "\n")
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-File", p],
                       capture_output=True, text=True)
    check(r.returncode == 0, "the probe runs: %s" % (r.stderr or "")[:400])
    return dict(l.split("|", 1) for l in r.stdout.strip().splitlines() if "|" in l)


# -- 1. the curve --------------------------------------------------------------------------------
AT = [-500, -1, 0, 1, 75, 150, 225, 299, 300, 301, 5000]
got = _ps(["$d = $script:DOCK_DUR_MS",
           "Write-Output ('dur|{0}' -f $d)",
           "foreach ($t in @(%s)) { Write-Output ('c{0}|{1:F6}' -f $t, (Dock-Curve $t)) }" %
           ",".join(str(t) for t in AT)])
dur = int(got["dur"])
c = dict((int(k[1:]), float(v)) for k, v in got.items() if k.startswith("c"))

check(c[-500] == 0.0 and c[-1] == 0.0 and c[0] == 0.0, "before it starts, nothing has moved")
check(c[300] == 1.0 and c[301] == 1.0 and c[5000] == 1.0, "after it ends, it stays put")
check(abs(c[150] - 0.5) < 1e-9, "the halfway point is halfway (got %r)" % c[150])
ordered = [c[t] for t in AT]
check(ordered == sorted(ordered), "it never goes backwards: %r" % ordered)

# Smoothstep, not a linear ramp: it has to leave and arrive at zero speed, or the dock visibly jerks
# into motion and stops dead. The first and last slices must move far less than the middle one.
first, mid, last = c[75] - c[0], c[225] - c[75], c[300] - c[225]
check(first < mid / 2, "it eases in - the first quarter moves %.3f against %.3f in the middle" % (first, mid))
check(last < mid / 2, "and eases out - the last quarter moves %.3f" % last)
check(abs(first - last) < 1e-9, "symmetrically (%.6f vs %.6f)" % (first, last))
print("curve: smoothstep over %dms, flat at both ends, monotonic" % dur)


# -- 2. two processes, one answer ------------------------------------------------------------------
# The whole design rests on this. Same flag, same clock, same function - so a tab and the meter in
# different processes compute the same offset without talking to each other.
flag = os.path.join(tmp, "dock.stow").replace("\\", "\\\\")
lines = ['$script:DockFlag = "%s"' % flag,
         '$script:dockChecked = 0',
         'Set-DockStowed $true',
         '$script:dockChecked = 0',
         '$now = NowMs',
         # Evaluate at instants we choose, not at "whenever this line ran".
         'foreach ($off in @(0,60,120,180,240,300,400)) {',
         '  Dock-Refresh',
         '  $k = Dock-Curve (($script:dockStart + $off) - $script:dockStart)',
         '  Write-Output ("o{0}|{1:F6}" -f $off, ($script:DOCK_TRAVEL * $k))',
         '}',
         'Write-Output ("travel|{0}" -f $script:DOCK_TRAVEL)']
a = _ps(lines)
b = _ps(lines)
shared = [k for k in a if k.startswith("o")]
check(len(shared) == 7, "got a sample per instant (%d)" % len(shared))
check(all(a[k] == b[k] for k in shared),
      "two separate processes agree exactly at every instant:\n  a=%r\n  b=%r"
      % ([a[k] for k in shared], [b[k] for k in shared]))
travel = float(a["travel"])
check(float(a["o0"]) == 0.0 and abs(float(a["o400"]) - travel) < 1e-9,
      "and the move runs the full travel (0 -> %r)" % travel)
print("determinism: two processes, 7 instants, identical to the last decimal")

# The move is scheduled slightly AHEAD of now, and that lead is what stops anyone joining late: an
# overlay polls the flag every DOCK_POLL_MS, so as long as the lead is longer than a poll, every
# overlay has seen the change before the first pixel moves. Without it the last one to notice would
# snap into position part-way through.
sched = _ps(['$script:DockFlag = "%s"' % flag,
             '$t0 = NowMs',
             'Set-DockStowed $false',
             '$p = ([PerPixelLayered]::ReadText($script:DockFlag)).Trim() -split "\\s+"',
             'Write-Output ("lead|{0}" -f ([int64]$p[1] - $t0))',
             'Write-Output ("poll|{0}" -f $script:DOCK_POLL_MS)',
             'Write-Output ("leadconst|{0}" -f $script:DOCK_LEAD_MS)'])
lead = int(sched["lead"])
poll = int(sched["poll"])
check(lead > 0, "the move is scheduled in the future, not for right now (got %+dms)" % lead)
check(lead >= poll, "by longer than a poll interval (%dms lead vs %dms poll)" % (lead, poll))
print("lead: scheduled %+dms ahead, against a %dms poll - nobody joins late" % (lead, poll))


# -- 3. the travel has to clear the widest tab ------------------------------------------------------
# Everything moves the same distance - that is the point - so that distance must be enough to push
# the widest possible chip past the screen edge, not merely the average one.
m = re.search(r"\$FORM_W = (\d+) \+ \$GLOW\*2", BADGE)
check(m is not None, "found the badge canvas width")
widest = int(m.group(1))
lane = int(re.search(r"\$DOCK_LANE = (\d+)", BADGE).group(1))
check(travel >= widest + lane,
      "travel %d clears the widest chip (%d) plus the lane (%d)" % (travel, widest, lane))
print("travel: %dpx, enough for a %dpx chip in a %dpx lane" % (travel, widest, lane))


# -- 4. the handle has to be clickable, which is a geometry problem --------------------------------
# This one is a real bug, not a hypothetical. A tab's glow reaches GLOW px beyond its chip, glow is
# not transparent, and a layered window is hit-tested on ALPHA rather than on ink - so a handle
# tucked under that glow is covered by a window that looks empty there, and never sees a click.
badge_glow = int(re.search(r"\$GLOW=(\d+)", BADGE).group(1))
hw = int(re.search(r"\$W = (\d+); \$H = \d+", DOCK).group(1))
check(lane - badge_glow >= hw,
      "the %dpx lane fits a %dpx handle clear of %dpx of tab glow (%d available)"
      % (lane, hw, badge_glow, lane - badge_glow))
check(re.search(r"\$DOCK_LANE = %d" % lane, METER),
      "and the meter leaves the same lane, or the two would not line up")
print("geometry: %dpx lane, %dpx of glow, %dpx handle - %dpx to spare"
      % (lane, badge_glow, hw, lane - badge_glow - hw))


# -- 5. nobody rolls their own ---------------------------------------------------------------------
check("Dock-Offset" in BADGE, "the tabs take the shared offset")
check("Dock-Offset" in METER, "so does the meter")
check("Dock-Phase" in DOCK, "and the handle turns on the same phase")
for name, src in (("badge.ps1", BADGE), ("hal_meter.ps1", METER), ("hal_dock.ps1", DOCK)):
    # A local spring toward a dock target is exactly what made them scatter.
    check(not re.search(r"Dock-Stowed\s*\)\s*\{[^}]*\}\s*else\s*\{[^}]*\}\s*\n\s*if \(\[Math\]::Abs", src),
          "%s does not ease its own way toward a dock target" % name)
check("$script:slide = Dock-Offset" in METER, "the meter assigns the shared offset outright")
# Twice in the handle: once at startup so it opens already turned the right way, and once per frame.
# Checking only that the string appears would let the per-frame one be deleted and never notice.
check(len(re.findall(r"\$script:flip = Dock-Phase", DOCK)) >= 2,
      "the handle takes the phase at startup AND every frame (found %d)"
      % len(re.findall(r"\$script:flip = Dock-Phase", DOCK)))
check(re.search(r"\$timer\.Add_Tick\((?:.|\n)*?\$script:flip = Dock-Phase", DOCK),
      "and the per-frame one is inside the tick, or the chevron would never turn")
# The tabs keep their own spring for the per-tab drawer, which is theirs alone - the dock offset is
# added on top of it rather than replacing it.
check(re.search(r"\$newDraw = \$script:chipX \+ \(Dock-Offset\)", BADGE),
      "a tab's drawer animation and the dock slide compose rather than fight")
check("$cx = [int]$script:drawX" in BADGE, "and the chip is drawn at the composed position")
print("wiring: one curve, three consumers, no local springs")


# -- 6. the case-insensitivity trap ------------------------------------------------------------------
# PowerShell variable names are case-insensitive, so a constant named $G and a Graphics object named
# $g are the same variable. The handle drew nothing at all until that was found.
check("$g = [System.Drawing.Graphics]" in DOCK, "the handle has a Graphics named $g")
code = "\n".join(re.sub(r"#.*$", "", l) for l in DOCK.splitlines())   # the comment explains the trap
check(not re.search(r"\$G(?![A-Za-z0-9_])", code),
      "and nothing else called $G, which would silently be the same variable")
print("naming: no constant collides with $g")

# -- 7. both hit boxes -------------------------------------------------------------------------------
# Two separate lessons about layered windows, learned the same way: what you can click is the alpha
# you drew, not the shape you meant.
#
# The handle runs off the right of its own canvas so the screen clips it. That leaves no visible
# right edge AND makes the last column of pixels on the screen part of the button - which is the
# only reason you can throw the pointer at the corner without aiming.
check(re.search(r"\$right = \$HG \+ \$W \+ \$HG", DOCK),
      "the handle's pill extends past its canvas, so it reaches the screen edge")
check(re.search(r"AddLine\(\$right, \$HG, \$right, \(\$HG \+ \$H\)\)", DOCK),
      "and the path actually uses that edge")
check(not re.search(r"InStrip[\s\S]{0,400}?\$cp\.X -lt", DOCK),
      "InStrip has no right-hand bound, or the very edge would be a dead column")

# The meter's readout is the button, not just the 62px of bar - the percentage and the countdown
# above it are the bigger target and were previously outside it entirely.
check("$script:hitL" in METER and "$script:hitR" in METER, "the meter records what it drew")
check(re.search(r"\$script:hitL = \$barR - \[Math\]::Max\(\$UW, \$hw\)", METER),
      "the box spans the bar or the reading, whichever is wider")
check(re.search(r"\$x0 = \$form\.Left \+ \$script:hitL; \$x1 = \$form\.Left \+ \$script:hitR", METER),
      "and the hit test reads it back rather than re-deriving a stale box")
check(not re.search(r"\$cp\.X -lt \(\$mx \+ \$UW\)", METER),
      "the old bar-only box is gone")
check(re.search(r"\$ha = if \(\$script:hot\) \{ 26 \} else \{ 3 \}", METER),
      "an all-but-invisible backing plate makes the gaps between glyphs clickable too - without it "
      "the readout looks like a button and behaves like a colander - and doubles as the hover cue")
check(re.search(r"RoundedPath \(\$script:hitL\)", METER), "and it covers exactly the hit box")
print("hit boxes: handle runs to the screen edge, meter covers its whole readout")


import shutil                            # noqa: E402
shutil.rmtree(tmp, ignore_errors=True)
print("\nOK - the dock leaves and returns as one object, and the handle can be hit")
