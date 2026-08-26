"""The daemon is the root of the HUD, so something has to notice when it dies.

It gives every open chat a tab, retires the ones that closed, and is the only thing that refreshes
the usage figures. It used to be watched by exactly one process - the usage meter - which the daemon
watched in return. Two processes minding each other is not supervision, and killing both inside a
few seconds left the HUD down until a hook happened to fire. That is not a thought experiment; it is
what happened while this was being built.

These cover the three latent bugs that would have made extra watchers useless or actively harmful,
and the invariants that keep the heartbeat, the staleness threshold and the two languages agreeing.
Nothing here starts a daemon: every spawn is intercepted.
"""
import os, re, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import Sandbox, check     # noqa: E402

import hal_common as hc                 # noqa: E402
import hal_badge as hb                  # noqa: E402
import hal_sessions as hs               # noqa: E402

SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")


# -- 1. the cadence invariant that bug (A) violated -----------------------------------------------
# The daemon beat once per loop and then slept for the whole poll interval. Idle, that interval is
# 12s against a 9s staleness threshold, so an idle daemon looked dead to every watcher for three
# seconds in every twelve - and each watcher would start a replacement, which then exited on the
# mutex. Nobody noticed because the only symptom was waste. Adding watchers multiplies it by N.
check(hs.BEAT_MS < hc.DAEMON_STALE_MS,
      "the heartbeat is faster than the threshold it is judged by (%d vs %d)"
      % (hs.BEAT_MS, hc.DAEMON_STALE_MS))
check(hs.BEAT_MS * 2 <= hc.DAEMON_STALE_MS,
      "with room to miss one beat and still look alive (%d vs %d)" % (hs.BEAT_MS, hc.DAEMON_STALE_MS))
for name, wait in (("busy", hs.POLL_MS), ("idle", hs.IDLE_POLL_MS)):
    # What the sleep loop actually does: sleep in BEAT_MS slices, beating after each.
    worst = min(hs.BEAT_MS, wait)
    check(worst < hc.DAEMON_STALE_MS,
          "the %s loop never goes quiet for longer than the threshold (%d)" % (name, worst))
check(hs.DAEMON_STALE_MS == hc.DAEMON_STALE_MS, "and there is one definition, not a copy")
print("cadence: beats every %ds against a %ds threshold, busy and idle alike"
      % (hs.BEAT_MS / 1000, hc.DAEMON_STALE_MS / 1000))


# -- 2. the two languages must agree about "dead" -------------------------------------------------
# Four copies of this number is how bug (A) survived. Python now has one; PowerShell keeps its own
# because it cannot import, so the copy is pinned here instead of merely commented.
common = open(os.path.join(SCRIPTS, "popup_common.ps1"), encoding="utf-8").read()
m = re.search(r"\$script:DaemonStaleMs\s*=\s*(\d+)", common)
check(m is not None, "popup_common.ps1 declares a staleness threshold")
check(int(m.group(1)) == hc.DAEMON_STALE_MS,
      "and it matches Python's (%s vs %d)" % (m.group(1), hc.DAEMON_STALE_MS))

# The spawn gate is what turns N watchers into one launch, and the daemon's own mutex is what makes
# a race merely wasteful rather than wrong. Both names are load-bearing; neither may drift.
check('"hal_session_daemon_spawn"' in common, "the PowerShell spawn gate has its own mutex name")
sessions_src = open(os.path.join(SCRIPTS, "hal_sessions.py"), encoding="utf-8").read()
check('"hal_session_daemon"' in sessions_src, "and the daemon still takes the singleton mutex")
check("hal_session_daemon_spawn" not in sessions_src,
      "which is a different name from the spawn gate, or the gate would block the daemon itself")
check("use_last_error=True" in sessions_src and "ctypes.get_last_error()" in sessions_src,
      "the mutex checks the error code in the way that actually preserves it")
print("thresholds: python %d == powershell %s, two distinct mutex names" % (hc.DAEMON_STALE_MS, m.group(1)))


# -- 3. every watcher can find the daemon, and they all use the shared one ------------------------
def _code(path):
    """The file with its comments stripped - a mention in prose is not a call site."""
    return "\n".join(re.sub(r"#.*$", "", l) for l in
                     open(path, encoding="utf-8").read().splitlines())


for f, why in (("badge.ps1", "one per open chat, and they outlive the daemon"),
               ("hal_tint.ps1", "a singleton second opinion"),
               ("hal_meter.ps1", "the original watcher, now sharing the implementation")):
    check(re.search(r"\b(Poll|Ensure)-HudDaemon\b", _code(os.path.join(SCRIPTS, f))) is not None,
          "%s actually calls the watchdog, not just mentions it (%s)" % (f, why))
check("function Ensure-SessionDaemon" not in
      open(os.path.join(SCRIPTS, "hal_meter.ps1"), encoding="utf-8").read(),
      "and the meter's private copy is gone, so there is one implementation to fix")
for fn in ("function Ensure-HudDaemon", "function Resolve-HalPython", "function Poll-HudDaemon"):
    check(fn in common, "popup_common.ps1 provides %s" % fn.split()[-1])
print("watchers: badge.ps1, hal_tint.ps1 and hal_meter.ps1, all on the shared helper")


# -- 4. _ensure_daemon: the two bugs that made the watchers inert --------------------------------
with Sandbox() as sb:
    ap = os.path.join(sb.badges, "sessions_daemon.alive")
    exe = os.path.join(sb.badges, "sessions_daemon.exe")
    spawned, reaped = [], []

    # The harness stubs _ensure_daemon to a no-op so no other suite ever starts a daemon; this is
    # the one suite that wants the real function, and the harness kept the original for us.
    real = sb._saved[(hb, "_ensure_daemon")]
    check(real is not hb._ensure_daemon, "got the real _ensure_daemon, not the harness stub")
    sb._set(hb.subprocess, "Popen", lambda a, **k: spawned.append(list(a)) or type("P", (), {"pid": 1})())
    sb._set(hb.subprocess, "run", lambda a, **k: reaped.append(list(a)))
    if os.name != "nt":
        print("skipping the spawn checks: _ensure_daemon is Windows-only by design")
    else:
        # (C) The interpreter path used to be written only from inside a daemon that had already
        # started - by the very process the PowerShell watchers were trying to start. On a fresh
        # machine, or once the file was deleted, they gave up silently at the moment they mattered.
        real()
        check(os.path.exists(exe), "starting the daemon records the interpreter for the watchers")
        check(open(exe, encoding="utf-8").read().strip(), "and it is not blank")
        check(len(spawned) == 1, "and it actually spawned one daemon (got %d)" % len(spawned))
        check(any("--daemon" in x for x in spawned[0]), "with --daemon (got %r)" % (spawned[0],))

        # A fresh heartbeat means hands off.
        del spawned[:]
        open(ap, "w").write("%d 4242" % int(time.time() * 1000))
        real()
        check(spawned == [], "a live daemon is left alone")

        # (B) A daemon that stopped beating while its process lives is wedged - and it still owns
        # the singleton mutex, so every replacement exits at startup and the respawn repeats for
        # ever. The overlays were always reaped here; the daemon never was.
        del spawned[:], reaped[:]
        stale = int(time.time() * 1000) - (hc.DAEMON_STALE_MS + 5000)
        open(ap, "w").write("%d %d" % (stale, os.getpid()))
        real()
        check(reaped and reaped[0][0] == "taskkill",
              "a wedged daemon is killed before a replacement is started (got %r)" % (reaped[:1],))
        check(str(os.getpid()) in reaped[0], "and it is the wedged pid that gets killed")
        check(len(spawned) == 1, "then a replacement starts (got %d)" % len(spawned))

        # A stale beat with no pid is a pre-mark by another watcher, not a wedge: nothing to kill.
        del spawned[:], reaped[:]
        open(ap, "w").write("%d 0" % stale)
        real()
        check(reaped == [], "a pre-marked file has no process to reap (got %r)" % reaped)
        check(len(spawned) == 1, "but is still replaced")
        print("_ensure_daemon: records the interpreter, reaps a wedge, skips a live one")

print("\nOK - the daemon is watched by everything on screen, and the watchers can actually see it")
