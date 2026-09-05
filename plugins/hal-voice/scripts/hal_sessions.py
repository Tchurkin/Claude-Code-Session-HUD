#!/usr/bin/env python3
"""
Session discovery + tab reconciler - "every open chat has a tab".

Hooks alone can't promise that. A hook only fires when a chat *does* something, so a chat that
is simply sitting there (never prompted since you opened it, or idle for hours) never announces
itself, and a tab whose window died stays dead until that chat is used again.

Claude Code itself keeps a registry of live sessions: ``~/.claude/sessions/<pid>.json`` holds
``{pid, sessionId, cwd, kind, startedAt, procStart}`` and is removed when the session exits. That
directory, filtered to PIDs that are genuinely still running, IS the set of open chats. This module
reconciles the HUD against it: adopt any live chat with no tab, respawn a tab whose window died,
rebind a tab whose window moved, and retire tabs whose chat is gone.

It runs two ways, both cheap: as a small always-on daemon (``--daemon``, one per machine, polls a
few times a minute) and once inline on every hook. So a missing tab self-heals within seconds
whichever path notices first.
"""
import glob, json, os, sys, tempfile, threading, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hal_common as hc
import hal_badge as hb
import hal_tokens
import hal_usage

# Claude Code's own state dir (relocatable via CLAUDE_CONFIG_DIR), where it registers sessions.
CLAUDE_DIR   = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(hc.HOME, ".claude")
SESSIONS_DIR = os.path.join(CLAUDE_DIR, "sessions")
PROJECTS_DIR = os.path.join(CLAUDE_DIR, "projects")

POLL_MS       = 4000      # daemon: how often to re-check which chats are open
IDLE_POLL_MS  = 12000     # daemon: slower beat while the HUD is switched off (nothing to draw)
BEAT_MS       = 3000      # daemon: heartbeat cadence - must stay well under DAEMON_STALE_MS
DAEMON_STALE_MS = hc.DAEMON_STALE_MS   # one definition, shared with the watchers (see hal_common)
RETIRE_MS     = 10000     # grace before a tab whose chat left the registry is closed
EMPTY_EXIT_MS = 120000    # no chats open at all for this long -> daemon exits (a hook respawns it)
LABEL_GAP_MS  = 6000      # spacing between background name lookups (they cost an LLM call)
LABEL_RETRY_MS = 300000   # don't re-attempt a name that just failed for 5 min
TITLE_EVERY   = 60000     # how often to re-read a chat's own title (used to find its window)
WINDOW_STALE_MS = 60000   # a window's self-report is dead if it hasn't heartbeated within this
WORKING_FRESH_MS = 90000  # a chat still counts as mid-turn if its state was touched this recently
WINDOWS_DIR   = os.path.join(hc.DATA_DIR, "windows")   # written by the companion VS Code extension
ALIVE_FILE    = os.path.join(hb.BADGE_DIR, "sessions_daemon.alive")
EXE_FILE      = os.path.join(hb.BADGE_DIR, "sessions_daemon.exe")   # interpreter, for PS-side respawn

_STILL_ACTIVE = 259
_OK_IMAGES = ("claude", "node", "bun", "deno")   # what a Claude Code session runs as


def _now():
    return int(time.time() * 1000)


# ── is that PID really still a Claude Code session? ────────────────────────────
def _win_proc(pid):
    """(alive, creation_filetime, image_basename) for a PID on Windows. Uses
    PROCESS_QUERY_LIMITED_INFORMATION so it works without elevation."""
    import ctypes
    from ctypes import wintypes
    k = ctypes.windll.kernel32
    k.OpenProcess.restype  = wintypes.HANDLE
    k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    h = k.OpenProcess(0x1000, False, int(pid))
    if not h:
        return (False, 0, "")
    try:
        code = wintypes.DWORD()
        if k.GetExitCodeProcess(h, ctypes.byref(code)) and code.value != _STILL_ACTIVE:
            return (False, 0, "")
        created = 0
        try:
            ft = [wintypes.FILETIME() for _ in range(4)]
            if k.GetProcessTimes(h, *[ctypes.byref(x) for x in ft]):
                created = (int(ft[0].dwHighDateTime) << 32) | (int(ft[0].dwLowDateTime) & 0xFFFFFFFF)
        except Exception:
            pass
        name = ""
        try:
            n = wintypes.DWORD(1024)
            buf = ctypes.create_unicode_buffer(1024)
            if k.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)):
                name = os.path.basename(buf.value or "").lower()
        except Exception:
            pass
        return (True, created, name)
    finally:
        try: k.CloseHandle(h)
        except Exception: pass


def _pid_alive(pid, proc_start=None):
    """Is this PID a running Claude Code session - and, when the registry recorded the process's
    creation time, is it still the SAME process? PIDs get recycled, and a recycled one would
    otherwise resurrect a closed chat's tab. Deliberately forgiving where we can't tell (a phantom
    tab is a far smaller sin here than a missing one)."""
    try:
        pid = int(pid)
    except Exception:
        return False
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)                       # POSIX: signal 0 is an existence check
            return True
        except Exception:
            return False
    try:
        alive, created, name = _win_proc(pid)
    except Exception:
        return True                               # can't probe -> assume the chat is still there
    if not alive:
        return False
    if proc_start:                                # exact identity check when the registry gave us one
        try:
            return int(created) == int(proc_start)
        except Exception:
            pass
    if name and not any(name.startswith(p) for p in _OK_IMAGES):
        return False                              # PID reused by something that isn't Claude Code
    return True


# ── which chats are open ───────────────────────────────────────────────────────
def registry_available():
    """Claude Code writes the session registry from 2.1-ish on. Without it we can't know what's
    open, and the badge falls back to its older hook-only lifecycle."""
    return os.path.isdir(SESSIONS_DIR)


def _proj_slug(cwd):
    """A cwd as Claude Code encodes it for ~/.claude/projects (every non-alphanumeric -> '-')."""
    return "".join(ch if ch.isalnum() else "-" for ch in str(cwd))


def transcript_for(sid, cwd):
    """This chat's transcript, for naming it and reading its context fill. Try the encoded project
    folder first, then fall back to a search (the encoding has changed before)."""
    if not sid:
        return None
    p = os.path.join(PROJECTS_DIR, _proj_slug(cwd), str(sid) + ".jsonl")
    if os.path.isfile(p):
        return p
    hits = glob.glob(os.path.join(PROJECTS_DIR, "*", str(sid) + ".jsonl"))
    return hits[0] if hits else None


_TMP_DIR = os.path.realpath(tempfile.gettempdir()).lower()


def _in_temp(cwd):
    """Is this chat running out of the temp dir? That's where we run our own headless `claude -p`
    naming calls from - they register like any other session for the seconds they live, and a tab
    that blinks into existence and straight back out is worse than no tab."""
    try:
        return os.path.realpath(str(cwd)).lower().startswith(_TMP_DIR)
    except Exception:
        return False


def discover():
    """Every Claude Code chat that is open right now, newest process per chat.

    A chat can appear under several PIDs (resume spawns a fresh process for the same sessionId
    while the old one is still winding down) - the newest wins, so one chat is one tab."""
    out = {}
    for f in glob.glob(os.path.join(SESSIONS_DIR, "*.json")):
        try:
            d = json.load(open(f, encoding="utf-8"))
        except Exception:
            continue
        sid, pid = d.get("sessionId"), d.get("pid")
        if not sid or not pid:
            continue
        kind = d.get("kind")
        if kind and kind != "interactive":
            continue                              # `claude -p` runs / other headless work: no tab
        cwd = str(d.get("cwd") or "")
        if _in_temp(cwd):
            continue                              # our own naming call, not one of your chats
        if not _pid_alive(pid, d.get("procStart")):
            continue
        started = 0.0
        try: started = float(d.get("startedAt") or 0)
        except Exception: pass
        prev = out.get(sid)
        if prev and prev["started"] >= started:
            continue
        out[sid] = {"sid": sid, "pid": int(pid), "cwd": cwd, "started": started,
                    "name": d.get("name") or "", "entry": d.get("entrypoint") or ""}
    return list(out.values())


# ── binding chats to windows ───────────────────────────────────────────────────
def _chat_title(sess):
    """This chat's own title, cached in its state file. Read from the transcript at most once a
    minute - titles change rarely, and the reconcile pass runs every few seconds."""
    sid = sess["sid"]
    st  = hb._read_state(sid)
    cur = st.get("title") or ""
    try:
        fresh = (_now() - float(st.get("title_ts") or 0)) < TITLE_EVERY
    except Exception:
        fresh = False
    if fresh:
        return cur          # asked recently - "this chat has no title" is an answer worth caching
    tp = transcript_for(sid, sess["cwd"])
    new = hb._ai_title(tp) if tp else ""
    if st:
        hb.update_state(sid, title=(new or cur), title_ts=_now())
    return new or cur


def _branch_shows(live):
    """Which tabs should carry their git branch.

    The branch is on a tab to tell parallel work apart - one chat per worktree, each on its own
    branch. It earns no space when it doesn't do that: two chats in the same folder on the same
    branch just get the same suffix twice, which is clutter on both. So a chat shows its branch
    unless another open chat is sitting in the same folder on the same branch."""
    counts = {}
    for s in live:
        key = (str(s["cwd"]).rstrip("/\\").lower(),
               (hb._read_state(s["sid"]).get("branch") or "").strip())
        counts[key] = counts.get(key, 0) + 1
    out = {}
    for s in live:
        branch = (hb._read_state(s["sid"]).get("branch") or "").strip()
        key = (str(s["cwd"]).rstrip("/\\").lower(), branch)
        out[s["sid"]] = bool(branch) and counts.get(key, 0) < 2
    return out


def _reported_windows():
    """What each VS Code window says it is holding, via the companion extension: every chat tab it
    has open and which one is in front. From outside we can only ever see the front one (that's all
    a window title says), so this is the only way to place a chat that's sitting in a background tab.
    Absent when the extension isn't installed - then we fall back to titles alone."""
    out, now = [], _now()
    for f in glob.glob(os.path.join(WINDOWS_DIR, "*.json")):
        try:
            d = json.load(open(f, encoding="utf-8"))
        except Exception:
            continue
        try:
            stale = (now - float(d.get("ts") or 0)) > WINDOW_STALE_MS
        except Exception:
            stale = True
        if stale:                                   # that window is gone; it heartbeats every 15s
            try: os.remove(f)
            except Exception: pass
            continue
        out.append(d)
    return out


def _reports_to_hwnd(reports, heads):
    """Pair each window's self-report with its OS window handle, through the one thing both know:
    the tab that's in front. Its label is what VS Code puts at the front of the window title."""
    pairs = []
    for rep in reports:
        if not hb._norm(rep.get("active")):
            continue
        for h, head in heads.items():
            if hb._same_chat(rep.get("active"), head):
                pairs.append((rep, h))
                break
    return pairs


def _bind_windows(live):
    """Pick the VS Code window each chat lives in, best evidence first.

    1. The window is *showing that chat* - its title leads with the chat's own name. Exact, and the
       only signal that works when a window's folder is shared by several chats or isn't in its
       title at all (a multi-root workspace reads "Untitled (Workspace)").
    2. Otherwise keep the window it's already bound to, while that window still exists - a chat you
       once identified shouldn't come unstuck the moment you click over to a source file and the
       window stops advertising its name.
    3. Otherwise a window whose title names its project folder, preferring one no other chat holds.

    Chats genuinely sharing a window all land on it, which is correct - one window is where they are.
    Returns {sid: (hwnd, showing)}, where `showing` is False for a chat whose window is currently
    displaying one of its neighbours instead - that's what stops all three tabs in a window lighting
    up at once as "the tab you're on".
    """
    binds, claimed, active_of, tab_of = {}, set(), {}, {}   # active_of: hwnd -> the chat it displays
    titles = hb._window_chat_titles()

    for rep, h in _reports_to_hwnd(_reported_windows(), titles):   # 0: the window told us itself
        tabs = rep.get("tabs") or []
        act  = rep.get("active")
        for s in live:
            t = _chat_title(s)
            if not t or s["sid"] in binds:
                continue
            hit = next((x for x in tabs if hb._same_chat(t, x)), None)
            if hit is not None:
                binds[s["sid"]] = h
                claimed.add(h)
                tab_of[s["sid"]] = hit      # its exact label, so a click can ask for it verbatim
                if hb._same_chat(t, act):
                    active_of[h] = s["sid"]
        if h not in active_of and act:
            active_of[h] = None                    # window is on a tab that isn't one of our chats

    for s in sorted(live, key=lambda s: s["started"]):        # 1: the window showing this chat
        t = _chat_title(s)
        if not hb._norm(t) or s["sid"] in binds:
            continue
        for h, head in titles.items():
            if h in claimed:
                continue
            if hb._same_chat(t, head):                        # window titles are truncated
                binds[s["sid"]] = h
                claimed.add(h)
                active_of[h] = s["sid"]
                break

    for s in live:                                            # 2: stick with what we had
        if s["sid"] in binds:
            continue
        h = 0
        try: h = int(hb._read_state(s["sid"]).get("hwnd") or 0)
        except Exception: pass
        if h and hb._is_vscode_window(h):
            binds[s["sid"]] = h
            claimed.add(h)

    for s in sorted(live, key=lambda s: s["started"]):        # 3: fall back to the folder name
        if s["sid"] in binds:
            continue
        wins = hb._find_chat_windows(s["cwd"])
        free = [w for w in wins if w not in claimed]
        h = (free or wins or [0])[0]
        if h:
            claimed.add(h)
        binds[s["sid"]] = h
    # A window whose active tab we identified is, right now, showing that one chat; where we
    # couldn't tell, every chat in the window stays lit as before rather than all going dark.
    return {sid: (h, active_of.get(h, sid) == sid, tab_of.get(sid, ""))
            for sid, h in binds.items()}


# ── naming an adopted chat ─────────────────────────────────────────────────────
def _cheap_label(sess):
    """An instant name for a chat we've just adopted: its project folder, tidied. No LLM - adoption
    must never block on the network - and a folder name is already the right kind of label, so a tab
    reads sensibly from the second it appears. ``_label_loop`` refines it to the area of work."""
    return hb._proj_label(sess.get("cwd")) or "Claude"


PACE_SID = "usage"      # pseudo-chat, so the warning card gets a pid file without inventing a tab
_IS_DAEMON = False      # set by daemon(); only the daemon raises usage alerts (see reconcile)


def _warn_pace(u, cfg):
    """The session's burn has just tipped into red: it will run out before the window resets.

    One chime and one card, at the moment it becomes true - which is the only moment when slowing
    down still changes the outcome. The card belongs to no chat, so clicking it just dismisses."""
    if not cfg.get("usage_alert", True):
        return
    hit = u.get("hit_mins")
    body = ("session limit in ~%d min at this rate" % hit) if hit is not None \
           else "on pace to run out before this window resets"
    shown = False
    if cfg.get("popup", True):
        shown = hb._spawn_popup("Usage - burning hot", body, color=hc.FAIL_COLOR,
                                hwnd=0, duration_ms=25000, chat="", sid=PACE_SID)
    if not shown and cfg.get("notify", True):
        hb.hal_notify.notify("Claude - usage", body)
    hb.beep_detached("attention")


# ── the reconcile pass ─────────────────────────────────────────────────────────
def reconcile():
    """Make the set of tabs equal the set of open chats. Returns the live chats, or None when the
    HUD is off / there's no registry to reconcile against."""
    cfg = hc.load_config()
    if not cfg.get("enabled", True) or not cfg.get("badge", True):
        return None                                   # HUD off: the overlays retire themselves
    if not registry_available():
        return None                                   # no registry (older CLI) -> hook-only lifecycle
    live = discover()
    now  = _now()
    os.makedirs(hb.BADGE_DIR, exist_ok=True)
    hb.ensure_helpers(cfg)          # the tint bar and the usage meter, if either has died
    binds = _bind_windows(live)
    shows = _branch_shows(live)
    seen  = set()
    busy  = False                   # is any chat mid-turn? decides how often to ask about usage

    for s in live:
        sid = s["sid"]
        seen.add(hb._sid8(sid))
        st   = hb._read_state(sid)
        # The freshness check is load-bearing: a chat killed mid-turn leaves "working" behind
        # forever, but its ts freezes with it, so a minute and a half later it stops counting.
        if st.get("state") == "working":
            try:
                busy = busy or (now - float(st.get("ts") or 0) < WORKING_FRESH_MS)
            except Exception:
                pass
        hwnd, showing, tabname = binds.get(sid) or (0, True, "")
        # Only write when something actually needs fixing - this runs every few seconds, forever.
        need = (not st                                                    # never had a tab
                or not st.get("label")                                    # unnamed
                or not hb._alive_fresh(sid)                               # its window died / was killed
                or (hwnd and int(st.get("hwnd") or 0) != hwnd)            # bound to the wrong window
                or list(st.get("color") or []) != list(hc.slot_color(st.get("slot") or 0))
                or st.get("gone") or st.get("ended_ts"))                  # was on its way out, came back
                                                                          # (resumed chats reuse the id)
        # A rostered folder is the authority on its own name. Inferred names drifted once the chats
        # started reading each other's work, and a wrong one would otherwise sit on the tab until
        # its next scheduled re-derive - half an hour of two tabs wearing the same label.
        pin = hb._pinned_label(s.get("cwd"))
        if st and pin and st.get("label") != pin:
            hb.update_state(sid, label=pin, label_src="roster", label_ts=now)

        brshow = shows.get(sid, False)
        if st and (bool(st.get("showing", True)) != showing      # which chat its window is on
                   or bool(st.get("branch_show", True)) != brshow  # whether its branch says anything
                   or (tabname and st.get("tab") != tabname)):   # (cheap: only when it changes)
            hb.update_state(sid, showing=showing, branch_show=brshow,
                            **({"tab": tabname} if tabname else {}))
        if not need:
            continue
        name = None if st.get("label") else _cheap_label(s)
        hb.touch(sid, s["cwd"], hwnd=hwnd, name=name,
                 label_src=("proj" if name else None), keep_ts=bool(st))
        if not st:                      # tab exists now, so its bookkeeping has somewhere to live
            _chat_title(s)              # cache the chat's title (the pre-touch read had nowhere to go)
            hb.update_state(sid, showing=showing, tab=tabname,
                            branch_show=_branch_shows(live).get(sid, False))

    if cfg.get("usage_meter", True):
        # Tokens come from the transcripts, so they cost nothing but disk and can be far fresher
        # than the plan's own figure - which is why they live in their own file on their own clock.
        tok = tpm = None
        if _IS_DAEMON:
            try:
                _t = hal_tokens.refresh() or {}                  # self-throttled to 5s, ~35ms
                tok, tpm = _t.get("total"), _t.get("tpm")
            except Exception:
                pass
        # The token total goes in so the reading can be carried forward between fetches; see
        # hal_usage.live_util. Passed rather than imported, to keep hal_usage free of dependencies.
        u = hal_usage.refresh(active=busy, tok_total=tok, tpm=tpm)
        # Only the daemon raises it. reconcile() also runs inline inside every hook, and two
        # processes reading `pace_alert` before either clears it would chime twice; there is exactly
        # one daemon (a named mutex sees to that), so gating here removes the race rather than
        # narrowing it. Costs at most one poll interval of delay, on a five-hour window.
        if _IS_DAEMON and u.get("pace_alert") and hal_usage.clear_alert():
            _warn_pace(u, cfg)               # cleared first: a crash must not re-chime on restart

    for f in glob.glob(os.path.join(hb.BADGE_DIR, "*.json")):
        sid8 = os.path.basename(f)[:-5]
        if sid8 in seen:
            continue
        _retire_if_gone(f, sid8, now)

    # Sweep drawer/order markers whose chat is long gone. `retire` clears a chat's own markers, so
    # these are only ever leftovers from older versions or from a state file removed by hand - but
    # they accumulate a byte at a time and there is no reason to keep them.
    for ext in ("stow", "ord"):
        for f in glob.glob(os.path.join(hb.BADGE_DIR, "*." + ext)):
            sid8 = os.path.basename(f)[:-(len(ext) + 1)]
            if not os.path.exists(os.path.join(hb.BADGE_DIR, sid8 + ".json")):
                try: os.remove(f)
                except Exception: pass

    try:                       # stamp the shared marker so hooks don't repeat this within seconds
        with open(hb.RECONCILE_MARK, "w") as fh:
            fh.write("%d %d" % (_now(), os.getpid()))
    except Exception:
        pass
    return live


def _retire_if_gone(path, sid8, now):
    """Close a tab whose chat is no longer open. Two guards against closing a live one on a bad
    read: the chat must have been quiet for a grace period, and must have been missing twice in a
    row (unless its SessionEnd hook already told us it's finished, in which case it goes at once)."""
    try:
        st = json.load(open(path, encoding="utf-8"))
    except Exception:
        return
    ended = bool(st.get("ended_ts"))
    if not ended:
        try:
            if now - float(st.get("ts") or 0) < RETIRE_MS:
                return
        except Exception:
            return
        if not st.get("gone"):                        # first sighting: mark, decide next pass
            st["gone"] = now
            try:
                tmp = path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump(st, fh)
                os.replace(tmp, path)
            except Exception:
                pass
            return
    hb.retire(sid8)


# ── background naming ──────────────────────────────────────────────────────────
def _needs_label(live):
    """The next adopted chat still wearing its placeholder name (oldest attempt first)."""
    best, best_ts = None, None
    for s in live:
        st = hb._read_state(s["sid"])
        if not st or st.get("label_src") == "llm":
            continue
        try:
            if _now() - float(st.get("label_try") or 0) < LABEL_RETRY_MS:
                continue
        except Exception:
            pass
        ts = float(st.get("label_try") or 0)
        if best_ts is None or ts < best_ts:
            best, best_ts = s, ts
    return best


def _llm_name(sess, tp):
    """Claude's 1-3 word name for this chat's area of work, or None if it couldn't be reached.

    Deliberately not ``_compute_topic``: that quietly falls back to the folder name, and a fallback
    wearing an LLM label would be recorded as 'named' and never retried - so a chat whose naming call
    timed out would keep a placeholder for as long as it stayed idle. Passes the folder and the
    neighbours' names, so chats sharing a repo get distinct labels."""
    try:
        msgs = hb._recent_messages(tp)
        if not msgs:
            return None
        cwd = sess.get("cwd")
        return hb._llm_topic(msgs, os.path.basename(str(cwd).rstrip("/\\")) if cwd else "",
                             hb._sibling_labels(sess["sid"], cwd))
    except Exception:
        return None


def _label_loop(stop, snapshot):
    """Upgrade placeholder names to Claude's 1-3 word summary, one at a time, on its own thread -
    the LLM call can take tens of seconds and the reconcile loop must never wait on it."""
    while not stop.is_set():
        stop.wait(LABEL_GAP_MS / 1000.0)
        if stop.is_set():
            break
        try:
            if not hc.load_config().get("enabled", True):
                continue
            s = _needs_label(list(snapshot))
            if not s:
                continue
            tp = transcript_for(s["sid"], s["cwd"])
            hb.mark_label_try(s["sid"])               # stamp first: a failure shouldn't retry hotly
            topic = _llm_name(s, tp) if tp else None
            if topic:                                 # no luck? keep the placeholder and try again later
                hb.touch(s["sid"], s["cwd"], name=topic, label_src="llm",
                         transcript_path=tp, keep_ts=True)
        except Exception:
            pass


# ── daemon ─────────────────────────────────────────────────────────────────────
def _singleton():
    """One daemon per machine. A named mutex is the only race-free guard on Windows (two hooks can
    spawn us in the same instant); elsewhere the heartbeat file is close enough."""
    if os.name == "nt":
        try:
            import ctypes
            # use_last_error, because plain ctypes.windll does not preserve GetLastError across the
            # call boundary - it can be clobbered by anything ctypes itself does on the way back. A
            # false "already exists" here means the daemon refuses to start for good while every
            # watcher keeps respawning it, which is the worst failure this file has.
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            h = k32.CreateMutexW(None, True, "hal_session_daemon")
            if not h or ctypes.get_last_error() == 183:                 # ERROR_ALREADY_EXISTS
                return False
            globals()["_MUTEX_HANDLE"] = h            # held for the process's lifetime
            return True
        except Exception:
            return True
    return not daemon_alive()


def daemon_alive():
    try:
        return (_now() - float(open(ALIVE_FILE).read().strip().split()[0])) < DAEMON_STALE_MS
    except Exception:
        return False


def _beat():
    try:
        with open(ALIVE_FILE, "w") as f:
            f.write("%d %d" % (_now(), os.getpid()))
    except Exception:
        pass


def daemon():
    if not _singleton():
        return
    globals()["_IS_DAEMON"] = True     # we are the one process allowed to raise usage alerts
    os.makedirs(hb.BADGE_DIR, exist_ok=True)
    _beat()
    try:
        with open(EXE_FILE, "w", encoding="utf-8") as f:      # so PS-side helpers can respawn us
            f.write(sys.executable or "python")
    except Exception:
        pass
    mtime = os.path.getmtime(__file__) if os.path.exists(__file__) else 0

    snapshot = []                                              # live chats, shared with the namer
    stop = threading.Event()
    namer = threading.Thread(target=_label_loop, args=(stop, snapshot), daemon=True)
    namer.start()

    empty_since = None
    try:
        while True:
            _beat()
            live = reconcile()
            if live is None:                                   # HUD off / no registry: idle politely
                snapshot[:] = []
                live = discover() if registry_available() else []
                wait = IDLE_POLL_MS
            else:
                snapshot[:] = live
                wait = POLL_MS
            if live:
                empty_since = None
            else:
                empty_since = empty_since or _now()
                if _now() - empty_since > EMPTY_EXIT_MS:
                    break                                      # nothing left to watch; hooks respawn us
            try:                                               # a plugin update should take effect
                if mtime and os.path.getmtime(__file__) != mtime:
                    break
            except Exception:
                pass
            # Beat while sleeping, not just once per pass. The idle wait is 12s and every watcher
            # calls anything older than 9s dead, so a single beat per iteration made an idle daemon
            # look like a corpse for three seconds out of every twelve - and each watcher would
            # helpfully start a replacement, which then exits on the mutex. Nobody noticed because
            # the waste is invisible; adding watchers would have multiplied it by their number.
            slept = 0
            while slept < wait:
                time.sleep(min(BEAT_MS, wait - slept) / 1000.0)
                slept += BEAT_MS
                _beat()
    finally:
        stop.set()
        try: os.remove(ALIVE_FILE)
        except Exception: pass


def main():
    if os.environ.get("HAL_SUPPRESS"):            # inside a `claude -p` we launched: stay out of it
        return
    arg = (sys.argv[1] if len(sys.argv) > 1 else "--once").lower()
    if arg == "--daemon":
        daemon()
    elif arg == "--list":                         # debugging: what does the HUD think is open?
        for s in sorted(discover(), key=lambda s: s["cwd"]):
            st = hb._read_state(s["sid"])
            print("%-10s pid %-6d %-28s %s" % (hb._sid8(s["sid"]), s["pid"],
                                               (st.get("label") or "-")[:28], s["cwd"]))
        print("%d open, %d with a tab" % (len(discover()),
                                          len(glob.glob(os.path.join(hb.BADGE_DIR, "*.json")))))
    else:
        reconcile()


if __name__ == "__main__":
    main()
