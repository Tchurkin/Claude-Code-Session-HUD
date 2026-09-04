"""The detail panel has to contain what it draws.

Every panel bug so far has been found by looking at a screenshot: a chart spanning two windows, and
then a chart running off the bottom of the panel entirely. Thirteen suites passed through both,
because they all test what the numbers are and none of them test where anything lands.

This tests the arithmetic that decides that. The panel's content grows with the number of chats
spending - four of them plus an Elsewhere row is five - and its height has to grow with it, or the
chart is drawn past the rounded background and clipped away by the edge of the canvas. That is
exactly what happened: the height was a constant while the content was not.

Extracts the real layout functions from hal_meter.ps1 and runs them, same as the colour ramp.
"""
import os, re, subprocess, sys, tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import check              # noqa: E402

SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
METER = open(os.path.join(SCRIPTS, "hal_meter.ps1"), encoding="utf-8").read()
tmp = tempfile.mkdtemp(prefix="hud-panel-")


def _const(name):
    m = re.search(r"\$%s\s*=\s*(\d+)" % re.escape(name), METER)
    check(m is not None, "found $%s in hal_meter.ps1" % name)
    return int(m.group(1))


def _fn(name):
    """One PowerShell function, by matching its braces."""
    i = METER.index("function %s" % name)
    depth, j = 0, METER.index("{", i)
    for k in range(j, len(METER)):
        if METER[k] == "{":
            depth += 1
        elif METER[k] == "}":
            depth -= 1
            if depth == 0:
                return METER[i:k + 1]
    raise AssertionError("unbalanced braces reading %s" % name)


PANEL_W = _const("PANEL_W")
PGLOW = _const("PGLOW")
SPARK_H = _const("PSPARK_H")
FOOT = _const("PANEL_FOOT")
ROW_H = _const("PANEL_ROW_H")

# Drive the real functions with a chosen number of rows, by setting the two script variables they
# read. Anything else would be re-implementing the layout in Python and testing my copy of it.
probe = os.path.join(tmp, "layout.ps1")
with open(probe, "w", encoding="utf-8") as f:
    for c in ("PANEL_ROW_Y", "PANEL_ROW_H", "PANEL_BARE_Y", "PANEL_FOOT", "PSPARK_H"):
        f.write("$%s = %d\n" % (c, _const(c)))
    f.write(_fn("Panel-Rows") + "\n" + _fn("Panel-ChartY") + "\n" + _fn("Panel-Height") + "\n")
    f.write("""
foreach ($n in 0..5) {
    $script:byChat = @()
    for ($i = 0; $i -lt $n; $i++) { $script:byChat += ,@("sid$i", 100) }
    $script:elsewhere = 0
    Write-Output ('{0}|{1}|{2}|{3}' -f $n, (Panel-Rows), (Panel-ChartY), (Panel-Height))
}
# ... and the same counts again with the Elsewhere row present, which is the case that overflowed.
foreach ($n in 0..4) {
    $script:byChat = @()
    for ($i = 0; $i -lt $n; $i++) { $script:byChat += ,@("sid$i", 100) }
    $script:elsewhere = 5000
    Write-Output ('e{0}|{1}|{2}|{3}' -f $n, (Panel-Rows), (Panel-ChartY), (Panel-Height))
}
""")
r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-File", probe],
                   capture_output=True, text=True)
check(r.returncode == 0, "the layout functions run: %s" % (r.stderr or "")[:400])
rows = {}
for line in r.stdout.strip().splitlines():
    if "|" in line:
        k, n, cy, h = line.split("|")
        rows[k] = (int(n), int(cy), int(h))
check(len(rows) == 11, "got a result per case (%d)" % len(rows))


# -- 1. the chart always fits inside the panel -----------------------------------------------------
# The whole bug in one assertion. The chart is drawn 14px under its label and is PSPARK_H tall; if
# that runs past the panel height it is simply clipped, with no error anywhere.
gaps = {}
for key, (n, chart_y, h) in sorted(rows.items()):
    bottom = chart_y + 14 + SPARK_H
    gaps[key] = h - bottom
    check(bottom <= h,
          "%s rows: chart ends at %d inside a %dpx panel (over by %d)" % (key, bottom, h, bottom - h))
tightest = min(gaps.values())
check(tightest >= FOOT,
      "and leaves the intended %dpx of foot under it (tightest was %d, at %r)"
      % (FOOT, tightest, [k for k, v in gaps.items() if v == tightest]))
print("fit: chart inside the panel at every row count, %dpx of foot to spare" % tightest)


# -- 2. the Elsewhere row counts toward the height -------------------------------------------------
# It is appended to the list after by_chat has already been capped, so it is the row that pushes the
# panel past a fixed height - and the one the old constant did not know about.
for n in range(5):
    plain = rows[str(n)]
    withel = rows["e%d" % n]
    check(withel[0] == plain[0] + 1, "%d chats plus Elsewhere is one more row (%r vs %r)" % (n, withel, plain))
    if n == 0:
        # An empty list is not a list of zero rows: with nothing to show, the chart's label moves up
        # to its own anchor instead. So the first row costs more than the ones after it.
        check(withel[2] > plain[2],
              "the first row still makes the panel taller (%d vs %d)" % (withel[2], plain[2]))
    else:
        check(withel[2] == plain[2] + ROW_H,
              "and each further row adds exactly its own height (%d vs %d)" % (withel[2], plain[2]))
print("elsewhere: counted as a row, and paid for in height")


# -- 3. the height genuinely varies -----------------------------------------------------------------
# Guards against the obvious regression: someone reintroducing a constant that happens to satisfy
# the fit check at every count by being large enough.
heights = sorted(set(h for _, _, h in rows.values()))
check(len(heights) >= 5, "the panel is a different height for different content (%r)" % heights)
check(rows["5"][2] > rows["0"][2] + 4 * ROW_H,
      "five rows is meaningfully taller than none (%d vs %d)" % (rows["5"][2], rows["0"][2]))
check("$PANEL_H" not in METER, "and no fixed panel height survives anywhere")
print("height: %s across 0..5 rows" % heights)


# -- 4. nothing else in the panel is left behind by a moving bottom ---------------------------------
# The bitmap, the rounded background, the placement and the click-away hit test all have to use the
# same height. Any one of them left on a constant is a panel whose visible edge and whose real edge
# disagree - which is a click that lands nowhere, or a background that stops before its content.
for what, pat in (("the bitmap", r"New-Object System\.Drawing\.Bitmap\(\(\$PANEL_W \+ \$PGLOW\*2\), \(\$script:panelH"),
                  ("the rounded background", r"RoundedPath \$ox \$oy \$PANEL_W \$script:panelH"),
                  ("where it is placed", r"\$script:lastTop \+ \$GLOW - 8 - \$script:panelH"),
                  ("the click-away test", r"\$panel\.Top \+ \$PGLOW \+ \$script:panelH")):
    check(re.search(pat, METER), "%s uses the computed height" % what)
check(re.search(r"\$renderPanel = \{\s*\n\s*\$script:panelH = Panel-Height", METER),
      "and the height is recomputed before every paint, not once at startup")
print("wiring: bitmap, background, placement and hit test all follow the same height")

import shutil                            # noqa: E402
shutil.rmtree(tmp, ignore_errors=True)
print("\nOK - the panel contains what it draws, at every row count")
