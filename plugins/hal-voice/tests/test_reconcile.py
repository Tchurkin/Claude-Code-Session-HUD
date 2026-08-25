"""
Isolated exercise of the reconciler: a synthetic session registry, a scratch state dir, no spawning.

The invariant under test is "every open chat has a tab, and only an open chat has a tab". This walks
the whole lifecycle of that promise: adopting chats that were already open before the HUD noticed
them, doing nothing at all when there is nothing to fix, bringing back a badge window that died,
retiring a tab only after its chat has been gone for a grace period AND missing twice, short-cutting
that wait when SessionEnd already said the chat is finished, giving tabs (and their colors) back to
chats that reopen, and standing down entirely when the HUD is off or there is no registry to
reconcile against.

The world is built with the Sandbox, so the result is the same on a laptop with twelve chats open
and on a CI box with none.
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import Sandbox, check

import time                     # noqa: E402
import hal_common as hc         # noqa: E402  (the harness put scripts/ on sys.path)
import hal_badge as hb          # noqa: E402
import hal_sessions as hs       # noqa: E402

# Chats must not live under the temp dir - `discover` skips those on purpose (that's where our own
# headless naming calls run from), and the Sandbox's own scratch root is in temp.
ROOT = os.path.join(os.path.abspath(os.sep), "hud-tests")

ALPHA = ("a1b2c3d4-1111-4f00-9000-000000000001", os.path.join(ROOT, "alpha-repo"))
BETA  = ("b2c3d4e5-2222-4f00-9000-000000000002", os.path.join(ROOT, "beta-tools-main"))
GAMMA = ("c3d4e5f6-3333-4f00-9000-000000000003", os.path.join(ROOT, "Gamma Site"))
DELTA = ("d4e5f6a7-4444-4f00-9000-000000000004", os.path.join(ROOT, "delta-lab"))
CHATS = [ALPHA, BETA, GAMMA, DELTA]


def show(sb, step):
    """One line per scenario, in the same shape the original printed: what tabs exist and how."""
    out = []
    for t in sb.tabs():
        d = sb.state(t)
        out.append("%s[%s|%s|slot%s%s]" % (t, (d.get("label") or "-")[:18], d.get("label_src"),
                                           d.get("slot"), " GONE" if d.get("gone") else ""))
    print("%-34s %d tabs  %s" % (step, len(out), " ".join(out)))


# usage_meter off: the reconcile pass otherwise asks api.anthropic.com how much of the plan is
# spent, which is a network call and has nothing to do with what's under test here.
with Sandbox(config={"usage_meter": False}) as sb:
    for sid, cwd in CHATS:
        sb.add_session(sid, cwd)
    # Two of them have been used; two have never been prompted, so they have no transcript at all -
    # exactly the chats a hook-only lifecycle can never see, and the reason this reconciler exists.
    sb.add_transcript(ALPHA[0], ALPHA[1], ["fix the parser"], ai_title="Parser Fix")
    sb.add_transcript(BETA[0], BETA[1], ["bump the deps"], ai_title="Dep Bump")

    # 1 ── cold start: no tabs at all, chats already open (the reported bug)
    live = hs.reconcile()
    check(live is not None, "the reconciler must run with the HUD on and a registry present")
    check(sorted(s["sid"] for s in live) == sorted(s for s, _ in CHATS),
          "the sandbox must present exactly the four open chats: %r" % (live,))
    print("live chats:", len(live), "| badge spawns:", len(sb.spawns))
    show(sb, "1. cold adopt")
    check(len(sb.tabs()) == len(live), "every open chat must get a tab")
    check(len(sb.spawns) == len(live), "every adopted tab must spawn a window")
    check(len(set(sb.state(t)["slot"] for t in sb.tabs())) == len(sb.tabs()),
          "colors must not collide")
    slots = dict((t, sb.state(t)["slot"]) for t in sb.tabs())

    # 2 ── steady state: windows alive, nothing to do -> no writes, no spawns
    sb.alive.update(sb.tabs())
    before = dict((t, os.path.getmtime(os.path.join(sb.badges, t + ".json"))) for t in sb.tabs())
    sb.spawns[:] = []
    writes = []
    real_write = hb._write_state
    hb._write_state = lambda sid, obj: (writes.append(hb._sid8(sid)), real_write(sid, obj))[1]
    time.sleep(0.05)                       # so a rewrite would show up as a different mtime
    try:
        hs.reconcile()
    finally:
        hb._write_state = real_write
    after = dict((t, os.path.getmtime(os.path.join(sb.badges, t + ".json"))) for t in sb.tabs())
    show(sb, "2. steady state")
    check(before == after, "steady-state pass must not rewrite state")
    check(writes == [], "steady-state pass must not touch any state file: %r" % (writes,))
    check(not sb.spawns, "steady-state pass must not respawn windows")

    # 3 ── a badge window dies -> it comes back on the next pass
    dead = hb._sid8(DELTA[0])
    sb.alive.discard(dead)
    sb.spawns[:] = []
    hs.reconcile()
    show(sb, "3. window killed")
    check(sb.spawns == [dead + ".json"], "the dead tab (only) is respawned: %s" % sb.spawns)
    check(len(sb.tabs()) == len(CHATS), "respawning must not disturb the other tabs")
    sb.alive.add(dead)

    # 4 ── a chat closes: registry drops it -> grace, then one 'gone' pass, then retired
    victim, victim_cwd = ALPHA
    v8 = hb._sid8(victim)
    hb.update_state(victim, ts=hs._now())              # it was doing something a moment ago
    sb.drop_session(victim)                            # ...and now its process is gone
    hs.reconcile(); show(sb, "4a. closed (within grace)")
    check(v8 in sb.tabs(), "must not retire a chat that only just went quiet")
    check(not sb.state(v8).get("gone"), "inside the grace period nothing is even marked")
    sb.age_tab(victim, hs.RETIRE_MS + 1000)            # age it past the grace period
    hs.reconcile(); show(sb, "4b. past grace (1st miss)")
    check(v8 in sb.tabs(), "one bad registry read must not close a tab")
    check(sb.state(v8).get("gone"), "the first miss must mark the tab, not retire it")
    hs.reconcile(); show(sb, "4c. past grace (2nd miss)")
    check(v8 not in sb.tabs(), "a closed chat's tab must go")
    check(len(sb.tabs()) == len(CHATS) - 1, "only the closed chat's tab goes")
    sb.alive.discard(v8)                               # its window closes with its state file

    # 5 ── SessionEnd short-circuits the wait
    victim2, victim2_cwd = BETA
    v28 = hb._sid8(victim2)
    hb.update_state(victim2, ts=hs._now())             # nowhere near the grace period...
    check(hs._now() - float(sb.state(v28)["ts"]) < hs.RETIRE_MS,
          "this tab must still be inside the grace period, or scenario 5 proves nothing")
    hb._mark_ended(victim2)                            # ...but its SessionEnd hook has fired
    sb.drop_session(victim2)
    hs.reconcile(); show(sb, "5. SessionEnd")
    check(v28 not in sb.tabs(), "SessionEnd + gone from the registry = retire at once")
    sb.alive.discard(v28)

    # 6 ── the chats come back (reopened): tabs return, colors are remembered
    # Beta reopens first, while the color it used to hold is NOT the lowest one going - so getting
    # it back can only be memory, never the fresh-slot arithmetic handing out the next free one.
    held = set(sb.state(t)["slot"] for t in sb.tabs())
    lowest_free = next(s for s in range(len(CHATS) + 2) if s not in held)
    check(slots[v28] != lowest_free,
          "scenario 6 only proves memory if beta's old color isn't the next one on offer")
    sb.spawns[:] = []
    sb.add_session(victim2, victim2_cwd)
    hs.reconcile(); show(sb, "6a. reopened (beta)")
    check(v28 in sb.tabs(), "a reopened chat gets its tab back")
    check(sb.spawns == [v28 + ".json"], "and a window with it: %s" % sb.spawns)
    check(sb.state(v28)["slot"] == slots[v28],
          "a chat that comes back keeps its own color, not the lowest one free")
    sb.alive.add(v28)

    sb.spawns[:] = []
    sb.add_session(victim, victim_cwd)
    hs.reconcile(); show(sb, "6b. reopened (alpha)")
    check(v8 in sb.tabs() and v28 in sb.tabs(), "reopened chats get their tabs back")
    check(sb.spawns == [v8 + ".json"], "only the returning chat spawns a window: %s" % sb.spawns)
    check(sb.state(v8)["slot"] == slots[v8], "and it keeps the color it had too")
    check(len(sb.tabs()) == len(CHATS), "every open chat has a tab again")
    check(len(set(sb.state(t)["slot"] for t in sb.tabs())) == len(sb.tabs()),
          "and still nobody shares a color")
    sb.alive.update(sb.tabs())

    # 7 ── HUD switched off -> reconcile is a no-op (overlays retire themselves)
    kept = sb.tabs()
    cfg_on = hc.load_config
    try:
        hc.load_config = lambda: dict(cfg_on(), enabled=False)
        check(hs.reconcile() is None, "HUD off -> nothing to reconcile")
        hc.load_config = lambda: dict(cfg_on(), badge=False)
        check(hs.reconcile() is None, "badges off -> nothing to reconcile")
    finally:
        hc.load_config = cfg_on
    check(sb.tabs() == kept, "a switched-off pass must not retire anything")
    print("%-34s %d tabs  (reconcile stood down)" % ("7. HUD off", len(sb.tabs())))

    # 8 ── no registry (older Claude Code) -> hands back to the legacy hook-only lifecycle
    kept = sb.tabs()
    hs.SESSIONS_DIR = os.path.join(sb.root, "nope")
    try:
        check(hs.registry_available() is False, "no sessions dir -> no registry")
        check(hs.reconcile() is None, "no registry -> nothing to reconcile against")
        check(sb.tabs() == kept, "legacy fallback must leave existing tabs alone")
        check(hb._registry_mode() is False, "no registry -> legacy lifecycle")
        # legacy dedupe is only ever consulted for a real window; an unknown one has no rival tabs
        check(hb._dedupe_window("x" * 8, 0x9999999) is True,
              "an unclaimed window's chat is its own keeper")
    finally:
        hs.SESSIONS_DIR = sb.registry
    print("%-34s %d tabs  (legacy lifecycle)" % ("8. no registry", len(sb.tabs())))

print("\nOK - reconcile adopts every open chat exactly once, writes nothing when there is nothing "
      "to fix, respawns only dead windows, retires a closed chat after grace + two misses (at once "
      "on SessionEnd), restores tabs and colors on reopen, and stands down with the HUD off or no "
      "registry")
