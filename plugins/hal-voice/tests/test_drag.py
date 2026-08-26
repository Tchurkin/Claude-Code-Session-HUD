"""Dragging a tab should push the others out of the way while you are still holding it.

It used to do nothing until you let go, and then rearrange the stack in one jump - so you were
choosing a position you could not see until you had committed to it.

Everything needed was already there: each badge eases toward whatever slot the shared ordering gives
it. Two things were missing. The dragged tab never published where it would currently land, and the
others only re-read the order every 600ms, which is slow enough to read as a shuffle rather than a
slide. This covers the signal that fixes the second, and pins the wiring for the first.

Dot-sources the real popup_common.ps1 and points it at a scratch directory; nothing is drawn.
"""
import os, re, subprocess, sys, tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import check              # noqa: E402

SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
COMMON_PS1 = os.path.join(SCRIPTS, "popup_common.ps1")
BADGE = open(os.path.join(SCRIPTS, "badge.ps1"), encoding="utf-8").read()

tmp = tempfile.mkdtemp(prefix="hud-drag-")

# -- 1. the drag signal ---------------------------------------------------------------------------
# A file whose TIMESTAMP is the whole message, so an idle badge noticing a drag costs one stat and
# never a read - it is checked on every frame of every badge, forever.
probe = os.path.join(tmp, "drag.ps1")
lines = [
    '. "%s"' % COMMON_PS1.replace("\\", "\\\\"),
    'if (-not $script:PplReady) { Write-Output "ready|no"; exit 1 }',
    'Write-Output "ready|yes"',
    '$script:PopupDir = "%s"' % tmp.replace("\\", "\\\\"),
    '$f = Join-Path $script:PopupDir "_drag.flag"',
    'Write-Output ("before|{0}" -f (Stack-DragActive))',
    'Stack-SignalDrag',
    'Write-Output ("exists|{0}" -f ([System.IO.File]::Exists($f)))',
    'Write-Output ("fresh|{0}" -f (Stack-DragActive))',
    '[System.IO.File]::SetLastWriteTimeUtc($f, [DateTime]::UtcNow.AddMilliseconds(-400))',
    'Write-Output ("recent|{0}" -f (Stack-DragActive))',
    '[System.IO.File]::SetLastWriteTimeUtc($f, [DateTime]::UtcNow.AddMilliseconds(-3000))',
    'Write-Output ("stale|{0}" -f (Stack-DragActive))',
    'Stack-SignalDrag',
    'Write-Output ("resignal|{0}" -f (Stack-DragActive))',
    '[System.IO.File]::Delete($f)',
    'Write-Output ("gone|{0}" -f (Stack-DragActive))',
    '$script:PopupDir = "Z:\\\\no\\\\such\\\\place"',
    'Write-Output ("nodir|{0}" -f (Stack-DragActive))',
]
with open(probe, "w", encoding="utf-8") as f:
    f.write("\n".join(lines) + "\n")
r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-File", probe],
                   capture_output=True, text=True)
check(r.returncode == 0, "the probe runs: %s" % (r.stderr or "")[:400])
got = dict(l.split("|", 1) for l in r.stdout.strip().splitlines() if "|" in l)

check(got.get("ready") == "yes", "popup_common dot-sources cleanly outside an overlay")
check(got["before"] == "False", "no flag, no drag")
check(got["exists"] == "True", "signalling writes the flag")
check(got["fresh"] == "True", "and it reads as active immediately")
check(got["recent"] == "True", "still active 400ms later, so a dropped frame does not end the drag")
check(got["stale"] == "False", "but not after three seconds - a killed badge cannot wedge it on")
check(got["resignal"] == "True", "and signalling again revives it")
check(got["gone"] == "False", "a deleted flag is not a drag")
check(got["nodir"] == "False", "nor is a directory that is not there")
print("signal: fresh -> active, 400ms -> active, 3s -> expired, missing -> quiet")


# -- 2. the flag must not be mistaken for a tab ----------------------------------------------------
# It lives in the same directory as the slot files, which several globs walk.
common = open(COMMON_PS1, encoding="utf-8").read()
import fnmatch                          # noqa: E402

check('"_drag.flag"' in common, "the flag has its own name")
STACK_DIRS = ("$script:PopupDir", "$ns")     # the two names the stack directory goes by
found = 0
for name in ("popup_common.ps1", "hal_meter.ps1", "badge.ps1"):
    src = open(os.path.join(SCRIPTS, name), encoding="utf-8").read()
    for var, pat in re.findall(r'GetFiles\((\$[\w:]+),\s*"([^"]+)"\)', src):
        if var not in STACK_DIRS:
            continue                     # a different directory entirely
        found += 1
        check(not fnmatch.fnmatch("_drag.flag", pat),
              "%s enumerates the stack dir as %r, which would pick up the drag flag" % (name, pat))
        # One of those globs deletes what it finds, which is why this matters rather than merely
        # being untidy: a flag swept as a stale slot would end every drag a frame after it began.
check(found >= 2, "found the stack enumerations to check (%d)" % found)
print("namespace: %d globs walk the stack directory, none of them can see the flag" % found)


# -- 3. the wiring on the badge side ----------------------------------------------------------------
# Where a tab would land is now worked out continuously, not only on release - and the same code
# does both, so the preview cannot disagree with the result.
check("$provisionalOrd" in BADGE, "the badge can work out where it would land")
check(len(re.findall(r"&\s*\$provisionalOrd", BADGE)) >= 2,
      "and uses it both while dragging and on drop, so the preview is the outcome")
check(re.search(r"if \(\$script:dragging\)(?:.|\n)*?Stack-SignalDrag", BADGE),
      "dragging signals the others to watch closely")
check(re.search(r"Stack-SignalDrag(?:.|\n){0,400}?\$script:StackOrd = & \$provisionalOrd", BADGE),
      "and republishes its provisional order as it moves")

# The other side: notice a drag quickly, but never re-read the order at speed when nothing is moving.
check(re.search(r"\$script:dragNear = Stack-DragActive", BADGE), "an idle badge checks for a drag")
check(re.search(r"if \(\$script:dragNear\) \{(?:.|\n)*?Stack-Sync", BADGE),
      "and only then re-reads the order out of turn")
check("$script:dragNear -or" in BADGE or "-or $script:dragNear" in BADGE,
      "and keeps its frame rate up while it happens, so the movement eases rather than steps")

# The drop still persists, or the whole thing would be a preview that forgets.
check(re.search(r"\$dropReorder = \{(?:.|\n)*?WriteAllText\(\$script:ordMarker", BADGE),
      "releasing still writes the order to disk")
print("wiring: one calculation for preview and drop, published while moving, read only when it is")

import shutil                            # noqa: E402
shutil.rmtree(tmp, ignore_errors=True)
print("\nOK - the stack gets out of the way while you drag, and costs a stat when you are not")
