"""
Background naming: how a tab gets its name, and how it stops getting it wrong.

A tab is adopted the instant its chat is discovered, so it can never wait on the network for a
name - it wears its project folder until Claude has read the conversation. This exercises that
upgrade path end to end against a synthetic world: four open chats (three sharing one repo, one on
its own), fake transcripts, and a stubbed namer that answers twice and then comes back empty.

Covered here:
  1. placeholder names come from the project folder, not a keyword salad
  2. names upgrade in the background, one chat at a time, without blocking or churning state
  3. a lookup that comes back empty is stamped as attempted, so it cools off instead of retrying hotly
  4. chats sharing a folder are told what their neighbours are called, so they get distinct names
  5. a folder-derived name is recorded as folder-derived, so it stays eligible for a real one later
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import Sandbox, check

import threading, time                                          # noqa: E402
import hal_badge as hb                                          # noqa: E402
import hal_sessions as hs                                       # noqa: E402

# Chats must not live under the temp dir - `discover` deliberately ignores those (that's where our
# own headless naming calls run from). So the world sits on a path that plainly isn't temp; nothing
# needs to exist on disk, only the transcripts the Sandbox writes for these sids.
WORK = os.path.join(os.path.abspath(os.sep), "hud-test-work")
REPO = os.path.join(WORK, "rocket-lab")        # three chats share this folder -> siblings
SOLO = os.path.join(WORK, "college-apps")      # one chat on its own -> no siblings

CHATS = [
    ("aaaa1111-1111-4111-8111-aaaaaaaaaaaa", REPO,
     ["work out the thrust curve for the F motor", "now size the fin can for it"]),
    ("bbbb2222-2222-4222-8222-bbbbbbbbbbbb", REPO,
     ["the flight computer keeps resetting mid-burn", "log the accelerometer over serial"]),
    ("cccc3333-3333-4333-8333-cccccccccccc", REPO,
     ["draft the recovery deployment checklist"]),
    ("dddd4444-4444-4444-8444-dddddddddddd", SOLO,
     ["help me shortlist which schools to apply to", "what does the essay prompt want"]),
]

# ── synthetic PID liveness ────────────────────────────────────────────────────
# The Sandbox registers one chat as this very process, which is what lets a synthetic entry pass
# the PID-liveness check. Four chats need four PIDs (the registry is one file per PID), so we teach
# the OS probe that our four fake PIDs are running Claude Code. The real `_pid_alive` logic - image
# name, exit code, PID-reuse guard - still runs; only the syscall underneath it is synthetic.
FAKE_PIDS = {}                                   # pid -> sid, purely for readability


def _install_fake_pids():
    if os.name == "nt":
        real = hs._win_proc

        def _win_proc(pid):
            if int(pid) in FAKE_PIDS:
                return (True, 0, "claude.exe")   # alive, no creation time, plausibly Claude Code
            return real(pid)

        hs._win_proc = _win_proc
        return ("_win_proc", real)
    real = hs._pid_alive

    def _pid_alive(pid, proc_start=None):
        return True if int(pid) in FAKE_PIDS else real(pid, proc_start)

    hs._pid_alive = _pid_alive
    return ("_pid_alive", real)


_restore = [_install_fake_pids(),
            ("LABEL_GAP_MS", hs.LABEL_GAP_MS),
            ("LABEL_RETRY_MS", hs.LABEL_RETRY_MS)]
_llm_topic_real = hb._llm_topic


def _snapshot_files(sb):
    """Every tab's state file, by content AND mtime - churn shows up as either."""
    out = {}
    for t in sb.tabs():
        p = os.path.join(sb.badges, t + ".json")
        with open(p, encoding="utf-8") as f:
            out[t] = (os.path.getmtime(p), f.read())
    return out


try:
    with Sandbox(config={"usage_meter": False}) as sb:          # no plan-usage fetch: no network
        # ── 1 ── adoption: a tab per open chat, wearing its project folder ────────────────────
        for i, (sid, cwd, texts) in enumerate(CHATS):
            pid = 900001 + i
            FAKE_PIDS[pid] = sid
            sb.add_session(sid, cwd, pid=pid)
            sb.add_transcript(sid, cwd, texts)

        live = hs.reconcile()
        check(len(live) == len(CHATS), "every open chat must be discovered: %r" % (live,))
        check(len(sb.tabs()) == len(CHATS), "every open chat must get a tab: %r" % (sb.tabs(),))
        sb.alive.update(sb.tabs())                              # their badge windows are up now

        adopted = {hb._sid8(sid): sb.state(sid)["label"] for sid, _, _ in CHATS}
        print("adopted:", len(sb.tabs()), "tabs |", " ".join(sorted(set(adopted.values()))))
        for sid, cwd, _ in CHATS:
            st = sb.state(sid)
            check(st["label"] == hb._proj_label(cwd),
                  "a fresh tab is named after its folder, not the transcript: %r" % (st,))
            check(st["label_src"] != "llm",
                  "a placeholder must not be filed as an LLM name: %r" % (st,))
        print("1. placeholders come straight from the folder - no LLM call was needed to adopt")

        # ── 2 ── the namer upgrades them in the background, one at a time ─────────────────────
        # The stub answers the first two lookups and then comes back empty, exactly like a namer
        # that can't reach Claude. Nothing here touches the network: `_llm_run` is stubbed by the
        # Sandbox and `_llm_topic` is replaced outright.
        calls, done = [], threading.Event()

        def _topic(msgs, proj="", siblings=()):
            calls.append({"proj": proj, "siblings": tuple(siblings), "msgs": len(msgs)})
            if len(calls) >= len(CHATS):
                done.set()                       # every chat has now been attempted
            return "Rocket Research" if len(calls) <= 2 else None

        hb._llm_topic = _topic

        snap = list(hs.discover())
        hs.LABEL_GAP_MS = 20                     # the real gap is 6s; the mechanism is the same
        stop = threading.Event()
        t = threading.Thread(target=hs._label_loop, args=(stop, snap), daemon=True)
        t.start()
        # Deterministic stop: the loop attempts each chat exactly once, then every chat is either
        # named or stamped-and-cooling, so it goes quiet on its own. We wait for that, not a clock.
        check(done.wait(60), "the namer must attempt every adopted chat: %d call(s)" % len(calls))
        stop.set()
        t.join(20)
        check(not t.is_alive(), "namer thread must be quiet before we snapshot for churn")

        named = [x for x in sb.tabs() if sb.state(x)["label_src"] == "llm"]
        tried = [x for x in sb.tabs() if sb.state(x).get("label_try", 0) > 0]
        print("2. LLM lookups:", len(calls), "| renamed:", len(named),
              "| attempts stamped:", len(tried), "of", len(sb.tabs()))
        check(1 <= len(named) <= 2, "only the answered lookups may rename a tab: %r" % (named,))
        check(len(named) == 2, "both answered lookups must land: %r" % (named,))
        check(len(calls) == len(tried), "every attempt is stamped, so an empty result cools off")
        check(len(calls) < len(sb.tabs()) + 2, "must not hammer the LLM: %d calls" % len(calls))
        check(len(calls) == len(sb.tabs()),
              "one lookup per chat - no chat asked about twice, none skipped: %d" % len(calls))
        for x in named:
            check(sb.state(x)["label"] == "Rocket Research",
                  "a renamed tab wears what the namer said: %r" % (sb.state(x),))

        before = _snapshot_files(sb)
        time.sleep(0.05)                          # so a rewrite would be visible in the mtime
        hs.reconcile()
        check(before == _snapshot_files(sb), "renaming must not churn")
        print("2. steady reconcile after renaming rewrote nothing")

        # ── 3 ── a lookup that came back empty cools off (and only cools off) ────────────────
        cooled = [x for x in sb.tabs() if sb.state(x)["label_src"] != "llm"]
        check(cooled, "the empty lookups must have left tabs on their placeholders")
        for x in cooled:
            st = sb.state(x)
            check(st.get("label_try", 0) > 0,
                  "a failed lookup is stamped as attempted: %r" % (st,))
            check(st["label"] == hb._proj_label(st.get("cwd")),
                  "a failed lookup keeps the placeholder: %r" % (st,))
        check(hs._needs_label(snap) is None,
              "nothing is due for another lookup: a failure must not retry in a tight loop")
        hs.LABEL_RETRY_MS = 0                     # ...but only until the cool-off elapses
        due = hs._needs_label(snap)
        check(due is not None and hb._sid8(due["sid"]) in cooled,
              "once cooled off, a failed chat is asked about again: %r" % (due,))
        hs.LABEL_RETRY_MS = _restore[2][1]
        print("3. failed lookups cooled off:", len(cooled),
              "| due again once the retry window passes:", hb._sid8(due["sid"]))

        # ── 4 ── every request carries the folder, and the neighbours' names ─────────────────
        check(all(c["proj"] for c in calls), "the project folder is always given to the namer")
        check(all(c["msgs"] > 0 for c in calls), "the namer reads the conversation")
        shared = [c for c in calls if c["siblings"]]
        print("4. requests carrying sibling names:", len(shared),
              "| e.g.", shared[0]["siblings"] if shared else None)
        check(shared, "chats sharing a folder must be told what their neighbours are called")
        check(all(c["proj"] == os.path.basename(REPO) for c in shared),
              "only the shared folder's chats have neighbours: %r" % (shared,))
        solo = [c for c in calls if c["proj"] == os.path.basename(SOLO)]
        check(solo and not any(c["siblings"] for c in solo),
              "a chat alone in its folder has no neighbours to be distinct from: %r" % (solo,))

        # placeholders before any LLM answer are folder names, not keyword salad
        placeholders = [sb.state(x)["label"] for x in sb.tabs() if sb.state(x)["label_src"] != "llm"]
        print("4. placeholder names:", placeholders)
        check(all(p and p[0].isupper() and len(p.split()) <= 4 for p in placeholders),
              "%r" % (placeholders,))
        print("OK - names upgrade in the background, once per chat, without blocking or churning")

        # ── 5 ── a name that fell back to the folder must be recorded as such ────────────────
        # Otherwise it looks named, and the namer never revisits it.
        sid0, cwd0 = snap[0]["sid"], snap[0]["cwd"]
        tp0 = hs.transcript_for(sid0, cwd0)
        check(tp0, "the fake chat must have a transcript to read")
        hb._llm_topic = lambda msgs, proj="", siblings=(): None       # Claude unreachable
        name, src = hb._compute_topic(tp0, cwd0, sid0)
        print("5. fallback name:", repr(name), "source:", src)
        check(src == "proj" and name, "a folder-derived name is not an LLM name")
        check(name == hb._proj_label(cwd0), "the fallback is the folder: %r" % (name,))
        hb._llm_topic = lambda msgs, proj="", siblings=(): "Rocket Research"
        check(hb._compute_topic(tp0, cwd0, sid0) == ("Rocket Research", "llm"),
              "an answered lookup is filed as an LLM name")
        print("OK - fallback names stay eligible for a real one later")

    print("OK - folder placeholders, one background rename per chat, empty lookups cooling off, "
          "sibling names passed to the namer, and fallbacks filed as folder-derived")
finally:
    hb._llm_topic = _llm_topic_real
    for attr, value in _restore:
        setattr(hs, attr, value)
