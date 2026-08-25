"""The branch belongs on a tab only when it tells two open chats apart.

A git branch on a tab is there for one job: telling parallel work apart when several chats are
open at once - one chat per worktree, each on its own branch. It earns no space when it doesn't
do that. Two chats sitting in the same folder on the same branch would just get the same suffix
twice, which is noise on both tabs.

Everything here is built out of the sandbox: synthetic tabs in a scratch state dir, hand-built
live-chat records of exactly the shape `hs.discover()` returns, and (for the end-to-end pass) a
synthetic session registry. Nothing depends on a real chat being open, a real repo existing on
disk, or a real desktop.
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import Sandbox, check

import hal_badge as hb          # noqa: E402  (_harness put scripts/ on sys.path)
import hal_sessions as hs       # noqa: E402

# Two folders that need not exist: _branch_shows only ever compares them as strings. The pair
# models the orchestrator layout the feature exists for - a repo and a worktree cut from it.
REPO     = r"C:\work\TVC PID Research"
WORKTREE = r"C:\work\TVC-wt-pyro"


def world(pairs):
    """Rebuild the whole HUD from scratch: [(sid, cwd, branch)] -> the live-chat list.

    The records are what `hs.discover()` hands the reconciler; the branch lives in each chat's tab
    state, which is where `_branch_shows` reads it from. Wiping first keeps every scenario
    independent of the one before it."""
    for f in os.listdir(hb.BADGE_DIR):
        try: os.remove(os.path.join(hb.BADGE_DIR, f))
        except OSError: pass
    live = []
    for sid, cwd, br in pairs:
        live.append({"sid": sid, "cwd": cwd, "started": 0, "name": "", "entry": ""})
        hb.touch(sid, cwd, name="X", label_src="kw")   # a tab exists for this chat
        hb.update_state(sid, branch=br)                # ...and it knows its branch
    return live


def brief(r):
    return {k[:8]: v for k, v in sorted(r.items())}


with Sandbox(config={"usage_meter": False}) as sb:     # no meter: it would want the network

    # 1 ── the reported case: two chats, same repo, same branch. The suffix would be identical on
    #      both tabs, so it distinguishes nothing and belongs on neither.
    live = world([("aaaaaaaa-1", REPO, "impulse22-sensor-bringup"),
                  ("bbbbbbbb-2", REPO, "impulse22-sensor-bringup")])
    r = hs._branch_shows(live)
    print("1. same repo, same branch  ->", brief(r))
    check(not any(r.values()), "a branch repeated on every tab in a repo says nothing")

    # 2 ── same repo, different branches: now the branch is the thing that tells them apart.
    live = world([("aaaaaaaa-1", REPO, "impulse22-sensor-bringup"),
                  ("bbbbbbbb-2", REPO, "main")])
    r = hs._branch_shows(live)
    print("2. same repo, split branch ->", brief(r))
    check(all(r.values()), "differing branches are worth showing")

    # 3 ── one chat per worktree - the orchestrator pattern the feature exists for.
    live = world([("aaaaaaaa-1", REPO, "main"), ("bbbbbbbb-2", WORKTREE, "pyro-fix")])
    r = hs._branch_shows(live)
    print("3. worktree per chat       ->", brief(r))
    check(all(r.values()), "a chat alone in its folder keeps its branch")

    # 4 ── no branch at all (the folder isn't a repo). Nothing to show, alone or not.
    live = world([("aaaaaaaa-1", REPO, "")])
    r = hs._branch_shows(live)
    print("4. no branch               ->", brief(r))
    check(not r["aaaaaaaa-1"], "nothing to show")
    live = world([("aaaaaaaa-1", REPO, "   ")])        # whitespace is not a branch either
    check(not hs._branch_shows(live)["aaaaaaaa-1"], "a blank branch is no branch")

    # 5 ── three in one repo, two sharing a branch: only the odd one out is worth labelling.
    live = world([("aaaaaaaa-1", REPO, "shared"), ("bbbbbbbb-2", REPO, "shared"),
                  ("cccccccc-3", REPO, "alone")])
    r = hs._branch_shows(live)
    print("5. two shared, one apart   ->", brief(r))
    check(r["cccccccc-3"] and not r["aaaaaaaa-1"] and not r["bbbbbbbb-2"],
          "only the chat whose branch is unique in its folder shows it")

    # 6 ── the same folder spelled differently is still the same folder. Windows hands us mixed
    #      case and stray separators; if that read as two folders, case 1 would leak the clutter
    #      it exists to prevent.
    live = world([("aaaaaaaa-1", REPO + "\\", "shared"),
                  ("bbbbbbbb-2", REPO.lower(), "shared")])
    r = hs._branch_shows(live)
    print("6. same folder, odd spelling ->", brief(r))
    check(not any(r.values()), "case and trailing slashes must not split one folder in two")

    # 7 ── end to end: the verdict has to reach the tab, not just the helper. A real reconcile
    #      pass over a registered, live chat must stamp branch_show onto its state file.
    sid = "eeeeeeee-1111-2222-3333-444444444444"
    world([(sid, REPO, "pyro-fix")])
    sb.add_session(sid, REPO)                  # this chat is genuinely open as far as discover cares
    sb.alive.add(hb._sid8(sid))                # ...and its badge window is up, so nothing needs repair
    hb.update_state(sid, branch_show=False)    # the stale/wrong answer the pass must correct
    check([s["sid"] for s in hs.discover()] == [sid], "the sandbox registry must yield exactly this chat")
    hs.reconcile()
    st = sb.state(sid)
    print("7. reconcile, alone w/ branch -> branch_show=%s branch=%r" % (st.get("branch_show"), st.get("branch")))
    check(st.get("branch_show") is True, "a lone chat's branch must be turned on by the reconciler")
    check(st.get("branch") == "pyro-fix", "the reconcile pass must not lose the branch itself")

    # ...and it must survive a tab being rebuilt, not just a quiet pass. Killing the badge window
    # sends the reconciler down the repair path, and that path rewrites the whole state file from
    # scratch - the branch has to come through it. Without this the check above could never fail:
    # nothing else here ever makes the reconciler rewrite a tab.
    sb.alive.discard(hb._sid8(sid))
    sb.spawns.clear()
    hs.reconcile()
    st = sb.state(sid)
    print("7b. tab rebuilt               -> spawns=%s branch=%r branch_show=%s"
          % (sb.spawns, st.get("branch"), st.get("branch_show")))
    check(sb.spawns == ["%s.json" % hb._sid8(sid)], "the dead tab must really have been rebuilt")
    check(st.get("branch") == "pyro-fix", "rebuilding a tab must not drop its branch")
    check(st.get("branch_show") is True, "...nor the verdict about whether to show it")
    sb.alive.add(hb._sid8(sid))                # window is back up; the next pass is a quiet one

    # ...and the same pass turns it back off once there is no branch to show.
    hb.update_state(sid, branch="", branch_show=True)
    hs.reconcile()
    st = sb.state(sid)
    print("8. reconcile, no branch       -> branch_show=%s" % st.get("branch_show"))
    check(st.get("branch_show") is False, "no branch means no suffix, whatever the tab used to say")
    check(sb.tabs() == [hb._sid8(sid)], "the pass must leave exactly the one open chat's tab")

print("\nOK - the branch shows up exactly when it distinguishes, and the reconciler writes that verdict onto the tab")
