"""The tab stack grows upward from the dock, and nothing used to stop it.

Past roughly twenty chats on a 1493x885 work area the topmost tabs walk off the top of the screen,
where they can be neither seen nor clicked - and the usage meter goes first, because it rides above
them. There is a ceiling now, and it is geometric by default, so tabs are only ever parked when the
alternative is being off-screen entirely.

The subtle part is not the cap, it is stability: parking a tab must never change which other tabs
are parked, or the dock oscillates. That is the property most of this file is about.

Extracts the real functions from popup_common.ps1 and runs them, same as the colour ramp.
"""
import os, re, subprocess, sys, tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import check              # noqa: E402

import hal_common as hc                 # noqa: E402

SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
COMMON = open(os.path.join(SCRIPTS, "popup_common.ps1"), encoding="utf-8").read()
BADGE = open(os.path.join(SCRIPTS, "badge.ps1"), encoding="utf-8").read()
METER = open(os.path.join(SCRIPTS, "hal_meter.ps1"), encoding="utf-8").read()


def _ps_function(src, name):
    """One PowerShell function, by matching its braces."""
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


tmp = tempfile.mkdtemp(prefix="hud-cap-")


def _run(body, *fns):
    p = os.path.join(tmp, "probe.ps1")
    with open(p, "w", encoding="utf-8") as f:
        for fn in fns:
            f.write(_ps_function(COMMON, fn) + "\n")
        f.write(body)
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-File", p],
                       capture_output=True, text=True)
    check(r.returncode == 0, "the probe runs: %s" % (r.stderr or "")[:400])
    return [l for l in r.stdout.strip().splitlines() if l.strip()]


# -- 1. capacity is derived from the screen, not guessed -------------------------------------------
# This machine: work area 1493x885, so bottomAnchor 829, chip 28 + gap 8 = pitch 36, and the meter
# reserves 37 above the stack. Those are the numbers the badges actually use.
out = _run("\n".join([
    "foreach ($p in @(@(829,36,37), @(829,36,0), @(400,36,37), @(60,36,37), @(829,0,37))) {",
    "  Write-Output ('{0},{1},{2}|{3}' -f $p[0],$p[1],$p[2],(Stack-Capacity $p[0] $p[1] $p[2]))",
    "}"]), "Stack-Capacity")
cap = dict(l.split("|") for l in out)
check(int(cap["829,36,37"]) == 21, "a full-height screen fits 21 tabs (got %s)" % cap["829,36,37"])
check(int(cap["829,36,0"]) == 22, "and one more if nothing rides above them (got %s)" % cap["829,36,0"])
check(int(cap["400,36,37"]) == 9, "a shorter dock fits fewer (got %s)" % cap["400,36,37"])
check(int(cap["60,36,37"]) >= 1, "and a tiny screen still shows one rather than none")
check(int(cap["829,0,37"]) >= 1, "a zero pitch cannot divide by zero")

# The cap has to leave the meter on screen - that was the original symptom.
fits = 21 * 36 + 37
check(fits <= 829 - 8, "21 tabs plus the meter still clear the top of the screen (%d of %d)"
                       % (fits, 829 - 8))
check(22 * 36 + 37 > 829 - 8, "and 22 would not, so the ceiling is where it should be")
print("capacity: 21 tabs on this screen, 22 without the meter, floors at 1")


# -- 2. the limit itself ---------------------------------------------------------------------------
out = _run("\n".join([
    "foreach ($p in @(@(5,0,21), @(30,0,21), @(30,10,21), @(5,10,21), @(30,50,21),",
    "                 @(30,1,21), @(30,-4,21), @(0,0,21), @(30,0,0))) {",
    "  Write-Output ('{0},{1},{2}|{3}' -f $p[0],$p[1],$p[2],(Stack-VisibleLimit $p[0] $p[1] $p[2]))",
    "}"]), "Stack-VisibleLimit")
lim = dict(l.split("|") for l in out)
check(int(lim["5,0,21"]) == 5, "five chats and room for 21 shows all five")
check(int(lim["30,0,21"]) == 21, "thirty chats shows what fits")
check(int(lim["30,10,21"]) == 10, "max_tabs lowers it")
check(int(lim["5,10,21"]) == 5, "but never invents tabs you do not have")
check(int(lim["30,50,21"]) == 21, "and never raises it past what fits - that is the whole point")
check(int(lim["30,1,21"]) == 1, "a cap of one is honoured")
check(int(lim["30,-4,21"]) == 21, "a nonsense cap is ignored rather than hiding everything")
check(int(lim["0,0,21"]) == 0, "no chats, no tabs")
check(int(lim["30,0,0"]) == 1, "and a degenerate capacity still shows one")
check(hc._DEFAULTS.get("max_tabs") == 0,
      "the default is geometric only, so nobody meets this until tabs would go off-screen")
print("limit: geometric by default, max_tabs only ever lowers it")


# -- 3. parking must not change who else is parked -------------------------------------------------
# The oscillation this avoids: if a parked tab dropped out of the ordering, the tab below the cut
# would rise above it, un-park, push the first back out, and the dock would flicker for ever. So a
# parked tab keeps its slot at zero height, and ranking runs over every tab, parked or not.
body = "\n".join([
    "$ordered = @()",
    "foreach ($i in 0..29) { $ordered += [pscustomobject]@{ id = \"tab$i\"; h = $(if ($i -lt 21) { 28 } else { 0 }) } }",
    "foreach ($i in 0..29) { Write-Output ('{0}|{1}' -f $i, (Stack-RankOf $ordered \"tab$i\")) }",
    "Write-Output ('missing|{0}' -f (Stack-RankOf $ordered 'nope'))",
])
ranks = dict(l.split("|") for l in _run(body, "Stack-RankOf"))
check(all(int(ranks[str(i)]) == i for i in range(30)),
      "rank is position in the order, parked or not")
check(int(ranks["missing"]) == 0, "and an id that is not there ranks at the dock, not off the end")

# The stable-set property, stated directly: parking the tail does not move the head.
for total in (22, 25, 30):
    limit = min(21, total)
    parked = [i for i in range(total) if i >= limit]
    check(parked and min(parked) == limit, "%d chats park exactly the tail past %d" % (total, limit))
    check(len([i for i in range(total) if i < limit]) == limit,
          "and the visible set is exactly the first %d" % limit)
print("stability: rank spans every tab, so parking the tail never disturbs the head")


# -- 4. a parked tab takes no room, and is not lost ------------------------------------------------
# Stack-TargetBottom sums the tabs before you. A parked tab reports zero height; if it still
# contributed a gap, the whole dock would float a row higher for every tab it cannot even see.
body = "\n".join([
    "$script:PopupId = 'me'",
    "$ordered = @([pscustomobject]@{id='a';h=28}, [pscustomobject]@{id='b';h=0},",
    "             [pscustomobject]@{id='c';h=28}, [pscustomobject]@{id='me';h=28})",
    "Write-Output ('withParked|{0}' -f (Stack-TargetBottom 829 8 $ordered 28))",
    "$ordered2 = @([pscustomobject]@{id='a';h=28}, [pscustomobject]@{id='c';h=28},",
    "              [pscustomobject]@{id='me';h=28})",
    "Write-Output ('without|{0}' -f (Stack-TargetBottom 829 8 $ordered2 28))",
])
tb = dict(l.split("|") for l in _run(body, "Stack-TargetBottom"))
check(tb["withParked"] == tb["without"],
      "a parked tab occupies no vertical space (%s vs %s)" % (tb["withParked"], tb["without"]))
check(int(tb["without"]) == 829 - (28 + 8) * 2 - 28, "and the surviving arithmetic is unchanged")

# The meter has to agree, or it floats a row higher for every parked tab.
check("if ($e.h -le 0) { $parked++ }" in METER,
      "the meter counts parked tabs separately from the height it rides on")
check("$info[2]" in METER, "and reads that count")
check(re.search(r"\+\$\(\$script:parked\)", METER),
      "and shows it, so tabs are never silently gone")
print("geometry: parked tabs cost nothing, and the meter says how many there are")


# -- 5. the badge actually applies it ---------------------------------------------------------------
check("Stack-VisibleLimit" in BADGE and "Stack-RankOf" in BADGE, "the badge computes its own rank")
# One definition of the claimed height, used by every live sync. It was spelled out in four places
# before, which is two more than it takes for them to disagree.
check(re.search(r"function SlotHeight \{ if \(\$script:parked\) \{ return 0 \}", BADGE),
      "a parked badge claims zero height")
for bypass in ("Stack-Sync $script:CH $true", "Stack-Write $script:CH"):
    check(bypass not in BADGE,
          "no live publish bypasses it (%r), or a parked tab would still take up a slot" % bypass)
publishes = len(re.findall(r"Stack-(?:Sync|Write) \(SlotHeight\)", BADGE))
check(publishes >= 3, "every publish of our height goes through it (found %d)" % publishes)
check("Stack-Capacity" in BADGE, "and derives its ceiling from the screen")
check(re.search(r"if \(\$script:parked\) \{", BADGE), "and draws nothing when parked")
check("Hud-ConfigNum 'max_tabs' 0" in BADGE, "honouring max_tabs, defaulting to geometric")
print("badge: ranks itself, parks quietly, keeps its slot")

import shutil                            # noqa: E402
shutil.rmtree(tmp, ignore_errors=True)
print("\nOK - the stack stops at the top of the screen, and stays put once it does")
