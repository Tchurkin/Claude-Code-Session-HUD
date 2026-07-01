#!/usr/bin/env python3
"""
Persistent per-chat color badge controller.

On activity a hook calls ``touch(...)``: it writes the badge's state (the chat's color,
a "what it's working on" label, the window to focus on click, and a working/done/awaiting
state) and, if no badge window is alive for that chat, spawns one (``badge.ps1``). The
window heartbeats ``<sid>.alive`` so we don't double-spawn, and it self-dismisses after
the chat has been idle for a while. Also runnable directly as a hook (reads session JSON
on stdin) - used for SessionStart / UserPromptSubmit.
"""
import glob, json, os, re, shutil, subprocess, sys, tempfile, time, urllib.request
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hal_common as hc
import hal_notify

# Filler words to ignore when deriving a theme without the LLM.
_STOP = set((
    "the a an to of for and or but in on at with by from as is are be it this that these "
    "those i im you we they my your our me us can could would should make made add also "
    "just so how do does did get got want need like use using new set change fix update "
    "them then than into out over under about please help lets let dont cant have has had "
    "not now here there when what which who why if else while thing things stuff way ok "
    "okay yes yeah nah see look take give go going one two three word words name names "
    "trying try build built tell think could change anything needs fixed really old long "
    "higher space corner move click covering across right wrong maybe good vibe still show "
    "across their its it's more less thats theres wanna gonna basically actually kinda "
    "vs of at by up an so no ok id re ve ll pm am"
).split())

BADGE_DIR   = os.path.join(hc.DATA_DIR, "badges")
BADGE_PS1   = os.path.join(hc.SCRIPTS_DIR, "badge.ps1")
TINT_PS1    = os.path.join(hc.SCRIPTS_DIR, "hal_tint.ps1")
BUTTON_PS1  = os.path.join(hc.SCRIPTS_DIR, "claude_button.ps1")
POPUP_PS1   = os.path.join(hc.SCRIPTS_DIR, "popup.ps1")
IDLE_MS     = 20 * 60 * 1000     # auto-dismiss after this much chat inactivity
TOPIC_EVERY = 90 * 1000          # recompute the "working on" label at most this often


def _sid8(session_id):
    s = "".join(ch for ch in str(session_id)[:8] if ch.isalnum())
    return s or "default"


def _state_path(sid): return os.path.join(BADGE_DIR, f"{_sid8(sid)}.json")
def _alive_path(sid): return os.path.join(BADGE_DIR, f"{_sid8(sid)}.alive")


def _alive_fresh(sid):
    try:
        return (time.time() * 1000 - float(open(_alive_path(sid)).read().strip())) < 4000
    except Exception:
        return False


def _read_state(sid):
    try:
        return json.load(open(_state_path(sid), encoding="utf-8"))
    except Exception:
        return {}


def _git_branch(cwd):
    """The chat's current git branch (its worktree/feature branch, when using an
    orchestrator) - shown on the badge so parallel sessions are distinguishable by branch."""
    if not cwd or not os.path.isdir(cwd):
        return ""
    try:
        r = subprocess.run(["git", "-C", cwd, "rev-parse", "--abbrev-ref", "HEAD"],
                           capture_output=True, text=True, timeout=2,
                           creationflags=hc.CREATE_NO_WINDOW)
        b = (r.stdout or "").strip()
        return b if b and b != "HEAD" else ""
    except Exception:
        return ""


def _foreground_hwnd():
    """The focused window ONLY if it's a VS Code window. Fallback for _find_chat_window."""
    try:
        import ctypes
        u = ctypes.windll.user32
        h = u.GetForegroundWindow()
        if not h:
            return 0
        n = u.GetWindowTextLengthW(h)
        buf = ctypes.create_unicode_buffer(n + 1)
        u.GetWindowTextW(h, buf, n + 1)
        return int(h) if (buf.value or "").rstrip().endswith("Visual Studio Code") else 0
    except Exception:
        return 0


def _find_chat_window(cwd):
    """The VS Code window whose title contains this chat's project folder - a far more
    reliable 'which window is this chat in' than whatever was foreground when the async
    hook ran (which mis-binds when several windows are open)."""
    proj = os.path.basename(str(cwd).rstrip("/\\")) if cwd else ""
    if not proj:
        return 0
    try:
        import ctypes
        from ctypes import wintypes
        u = ctypes.windll.user32
        match = []

        @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
        def _cb(h, _l):
            if not u.IsWindowVisible(h):
                return True
            n = u.GetWindowTextLengthW(h)
            if n <= 0:
                return True
            buf = ctypes.create_unicode_buffer(n + 1)
            u.GetWindowTextW(h, buf, n + 1)
            t = buf.value or ""
            if t.endswith("Visual Studio Code") and proj in t:
                match.append(int(h))
                return False
            return True

        u.EnumWindows(_cb, 0)
        return match[0] if match else 0
    except Exception:
        return 0


def _window_title(hwnd):
    try:
        import ctypes
        u = ctypes.windll.user32
        n = u.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(n + 1)
        u.GetWindowTextW(hwnd, buf, n + 1)
        return buf.value or ""
    except Exception:
        return ""


def _hwnd_ok(hwnd, proj):
    """The stored handle is still a live VS Code window for this chat's project."""
    if not hwnd:
        return False
    try:
        if not ctypes_is_window(hwnd):
            return False
    except Exception:
        return False
    t = _window_title(hwnd)
    return t.endswith("Visual Studio Code") and (not proj or proj in t)


def ctypes_is_window(hwnd):
    import ctypes
    return bool(ctypes.windll.user32.IsWindow(hwnd))


def _capture_hwnd(cwd):
    """Best guess at this chat's VS Code window: the focused window when it belongs to this
    project (you just typed there), else the window whose title names the project, else the
    focused VS Code window."""
    proj = os.path.basename(str(cwd).rstrip("/\\")) if cwd else ""
    fg = _foreground_hwnd()                       # focused VS Code window or 0
    if fg and (not proj or proj in _window_title(fg)):
        return fg
    return _find_chat_window(cwd) or fg


# ── "working on" label from the recent transcript ──────────────────────────────
def _tail_lines(path, maxbytes=262144):
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - maxbytes))
            data = f.read()
        return data.decode("utf-8", "ignore").splitlines()[-400:]
    except Exception:
        return []


def _extract_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text" and b.get("text"):
                out.append(b["text"])
            elif isinstance(b, str):
                out.append(b)
        return " ".join(out)
    return ""


def _recent_messages(transcript_path, n=20):
    msgs = []
    for ln in _tail_lines(transcript_path):
        try:
            o = json.loads(ln)
        except Exception:
            continue
        m = o.get("message") or o
        role = m.get("role") or o.get("type")
        if role not in ("user", "assistant"):
            continue
        text = _extract_text(m.get("content")).strip()
        if text and not text.startswith("<"):     # skip system-reminder/tool-noise blocks
            msgs.append((role, text))
    return msgs[-n:]                               # ~10 exchanges (user+assistant)


def _short(s, n):
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[:n - 1].rstrip() + "…"


def _anthropic_key():
    k = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    if k:
        return k
    for p in ("~/.claude/.anthropic_key", "~/.anthropic_key"):
        try:
            v = open(os.path.expanduser(p)).read().strip()
            if v:
                return v
        except Exception:
            pass
    return None


def _openai_key():
    k = (os.environ.get("OPENAI_API_KEY") or "").strip()
    if k:
        return k
    for p in ("~/.claude/.openai_key", "~/.openai_key"):
        try:
            v = open(os.path.expanduser(p)).read().strip()
            if v:
                return v
        except Exception:
            pass
    return None


_CLAUDE_EXE = None
_CLAUDE_EXE_DONE = False


def _claude_exe():
    """Locate the Claude Code CLI so we can name tabs via the user's existing Claude Code login
    (no API key, uses their subscription). Prefers a `claude` on PATH; otherwise the binary
    bundled inside the VS Code / Cursor extension, newest install winning. Cached per process."""
    global _CLAUDE_EXE, _CLAUDE_EXE_DONE
    if _CLAUDE_EXE_DONE:
        return _CLAUDE_EXE
    _CLAUDE_EXE_DONE = True
    exe = shutil.which("claude")
    if exe:
        _CLAUDE_EXE = exe
        return exe
    cands = []
    for d in (".vscode", ".vscode-insiders", ".vscode-server", ".cursor", ".windsurf"):
        root = os.path.expanduser(os.path.join("~", d, "extensions"))
        for nm in ("claude.exe", "claude"):
            cands += glob.glob(os.path.join(root, "anthropic.claude-code-*", "resources", "native-binary", nm))
    if cands:
        try:
            _CLAUDE_EXE = max(cands, key=os.path.getmtime)     # newest-installed extension
        except Exception:
            _CLAUDE_EXE = cands[-1]
    return _CLAUDE_EXE


def _claude_cli_topic(prompt):
    """Name the tab through the user's Claude Code login (no API key). Runs headless in a temp
    cwd (so it won't load the project's context) with HAL_SUPPRESS set so its own hooks no-op."""
    exe = _claude_exe()
    if not exe:
        return None
    try:
        env = dict(os.environ, HAL_SUPPRESS="1")
        r = subprocess.run(
            [exe, "-p", prompt, "--model", "claude-haiku-4-5-20251001"],
            capture_output=True, text=True, timeout=18, env=env,
            cwd=tempfile.gettempdir(), creationflags=hc.CREATE_NO_WINDOW)
        t = (r.stdout or "").strip()
        if t:
            t = t.splitlines()[0].strip().strip('."\'').strip()
        if t and len(t) <= 40:
            return _short(t, 30)
    except Exception:
        pass
    return None


def _llm_topic(msgs):
    """1-3 word theme of the RECENT focus. This is a Claude Code plugin, so it uses Claude: an
    Anthropic API key if set, else the local Claude Code CLI (your subscription, no key). OpenAI
    is opt-in only (config `use_openai`). Reflects current work so a chat re-labels on a shift."""
    convo = "\n".join(f"{r}: {t[:220]}" for r, t in msgs)[-4200:]
    prompt = ("Below are recent messages from an ongoing coding chat, oldest to newest:\n\n"
              + convo +
              "\n\nName what this chat is currently working on in 1 to 3 words (Title Case, no "
              "quotes or punctuation), reflecting the RECENT focus. Reply with ONLY the phrase.")
    akey = _anthropic_key()
    if akey:
        try:
            import anthropic
            m = anthropic.Anthropic(api_key=akey, timeout=6.0).messages.create(
                model="claude-haiku-4-5-20251001", max_tokens=12,
                messages=[{"role": "user", "content": prompt}])
            t = m.content[0].text.strip().strip('."\'').strip()
            if t:
                return _short(t, 30)
        except Exception:
            pass
    t = _claude_cli_topic(prompt)          # your existing Claude Code login - no API key needed
    if t:
        return t
    if hc.load_config().get("use_openai", False) and _openai_key():   # ChatGPT: opt-in, off by default
        key = _openai_key()
        try:
            body = json.dumps({"model": "gpt-4o-mini", "max_tokens": 12, "temperature": 0.3,
                               "messages": [{"role": "user", "content": prompt}]}).encode("utf-8")
            req = urllib.request.Request(
                "https://api.openai.com/v1/chat/completions", data=body,
                headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=7) as resp:
                d = json.loads(resp.read().decode("utf-8"))
            t = d["choices"][0]["message"]["content"].strip().strip('."\'').strip()
            if t:
                return _short(t, 30)
        except Exception:
            pass
    return None


def _keyword_from_text(text):
    """No-LLM fallback: the top 2-3 meaningful words in `text`, in first-seen order."""
    words = [w for w in re.findall(r"[a-z][a-z0-9+]+", (text or "").lower()) if w not in _STOP]
    if not words:
        return None
    common = set(w for w, _ in Counter(words).most_common(3))
    seen, ordered = set(), []
    for w in words:
        if w in common and w not in seen:
            seen.add(w); ordered.append(w)
    return " ".join(w.upper() if len(w) <= 3 else w.capitalize() for w in ordered)


def _first_user_message(transcript_path):
    """The chat's opening user ask - a stable statement of what the chat is FOR."""
    try:
        with open(transcript_path, "rb") as f:
            head = f.read(60000)
    except Exception:
        return None
    for ln in head.decode("utf-8", "ignore").splitlines():
        try:
            o = json.loads(ln)
        except Exception:
            continue
        m = o.get("message") or o
        if (m.get("role") or o.get("type")) == "user":
            t = _extract_text(m.get("content")).strip()
            if t and not t.startswith("<") and len(t) > 8:
                return t
    return None


def _compute_topic(transcript_path):
    # A coherent 1-3 word theme: the LLM summary if a key is available; else a keyword theme
    # from the chat's opening ask (its purpose), then recent messages. Never the raw last msg.
    msgs = _recent_messages(transcript_path)
    if msgs:
        t = _llm_topic(msgs)
        if t:
            return t
    return (_keyword_from_text(_first_user_message(transcript_path))
            or _keyword_from_text(" ".join(t for r, t in msgs if r == "user")))


# ── lifecycle ──────────────────────────────────────────────────────────────────
def _ensure_singleton(name, ps1):
    """Keep one global helper window (window tint / Claude button) running. The .ps1 also
    guards itself with a named mutex, so this only avoids repeated spawn attempts."""
    ap = os.path.join(BADGE_DIR, name + ".alive")
    try:
        if time.time() * 1000 - float(open(ap).read().strip()) < 4000:
            return
    except Exception:
        pass
    try:
        with open(ap, "w") as f:
            f.write(str(time.time() * 1000))
    except Exception:
        pass
    try:
        subprocess.Popen(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", ps1, "-AliveFile", ap],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=hc.CREATE_NO_WINDOW)
    except Exception:
        pass


def _gc_stale():
    # Only reap state whose badge is NOT running (no fresh heartbeat) and has since gone quiet. A
    # live badge - even an idle one whose window is still open - keeps its state so the tab persists.
    now = time.time() * 1000
    for f in glob.glob(os.path.join(BADGE_DIR, "*.json")):
        sid8 = os.path.basename(f)[:-5]
        if _alive_fresh(sid8):
            continue
        try:
            if now - float(json.load(open(f, encoding="utf-8")).get("ts", 0)) > IDLE_MS:
                for p in (f, os.path.join(BADGE_DIR, sid8 + ".alive")):
                    try: os.remove(p)
                    except Exception: pass
        except Exception:
            pass


def _active_slots(exclude_sid):
    """Color slots currently held by other, still-active sessions."""
    now  = time.time() * 1000
    ex   = _sid8(exclude_sid)
    used = set()
    for f in glob.glob(os.path.join(BADGE_DIR, "*.json")):
        sid8 = os.path.basename(f)[:-5]
        if sid8 == ex or not _alive_fresh(sid8):
            continue                                   # only a session with a live badge holds a color
        try:
            s = json.load(open(f, encoding="utf-8")).get("slot")
        except Exception:
            continue
        if s is not None:
            try: used.add(int(s))
            except Exception: pass
    return used


def _assign_slot(session_id):
    """The lowest slot not already taken by another open session, so a new chat gets the most
    different color still available. Slots free up as sessions go idle and are GC'd, so the
    palette stays tight around however many chats are actually live."""
    used = _active_slots(session_id)
    slot = 0
    while slot in used:
        slot += 1
    return slot


def _dedupe_window(session_id, hwnd):
    """One badge per VS Code window: keep the most-recently-active session for a given
    window and retire the rest (older tabs / churned or leftover sessions). Returns True if
    `session_id` is the keeper."""
    now = time.time() * 1000
    recent = []
    for f in glob.glob(os.path.join(BADGE_DIR, "*.json")):
        try:
            d = json.load(open(f, encoding="utf-8"))
        except Exception:
            continue
        sid8 = os.path.basename(f)[:-5]
        if int(d.get("hwnd") or 0) == hwnd and _alive_fresh(sid8):
            recent.append((float(d.get("ts", 0)), sid8, f))
    if len(recent) <= 1:
        return True
    keeper = max(recent)[1]                          # newest ts owns this window's badge
    for ts, sid8, f in recent:
        if sid8 != keeper:
            for p in (f, os.path.join(BADGE_DIR, sid8 + ".alive")):
                try: os.remove(p)
                except Exception: pass
    return _sid8(session_id) == keeper


def touch(session_id, cwd=None, capture_hwnd=False, state=None, transcript_path=None, reason=None):
    """Refresh this chat's badge state and ensure its window is running.

    state: 'working' | 'done' | 'waiting' (drives the on-badge indicator).
    transcript_path: when given (SessionStart/UserPromptSubmit), refresh the label.
    capture_hwnd: record which window to focus on click (only at moments the user is here)."""
    cfg = hc.load_config()
    if not session_id or not cfg.get("badge", True):
        return
    os.makedirs(BADGE_DIR, exist_ok=True)
    now  = int(time.time() * 1000)
    prev = _read_state(session_id)
    slot = prev.get("slot")
    if slot is None:
        slot = _assign_slot(session_id)              # first sighting -> claim the most distinct free color
    r, g, b = hc.slot_color(slot)
    proj   = os.path.basename(str(cwd).rstrip("/\\")) if cwd else ""
    prev_h = int(prev.get("hwnd") or 0)
    if capture_hwnd:
        hwnd = _capture_hwnd(cwd) or prev_h          # user is here -> prefer the focused window
    elif _hwnd_ok(prev_h, proj):
        hwnd = prev_h                                # stored handle still valid -> keep it
    else:
        hwnd = _find_chat_window(cwd) or prev_h      # stale/wrong -> re-find by project title

    label    = prev.get("label") or ""
    label_ts = float(prev.get("label_ts") or 0)
    if transcript_path and (not label or now - label_ts > TOPIC_EVERY):
        topic = _compute_topic(transcript_path)
        if topic:
            label, label_ts = topic, now
    if not label:
        label = os.path.basename(str(cwd).rstrip("/\\")) if cwd else ""

    st = state or prev.get("state") or "done"
    branch = _git_branch(cwd) if capture_hwnd else (prev.get("branch") or "")   # feature/worktree branch
    reason_val = _short(reason, 30) if reason else (prev.get("reason") or "")    # what it's waiting on
    present_ts = now if capture_hwnd else float(prev.get("present_ts") or 0)      # last time the user was here
                                                                                 # (drives un-hiding a dismissed tab)
    sp = _state_path(session_id)
    try:
        tmp = sp + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"ts": now, "color": [r, g, b], "slot": slot, "label": label, "hwnd": hwnd,
                       "state": st, "label_ts": label_ts, "branch": branch, "reason": reason_val,
                       "present_ts": present_ts}, f)
        os.replace(tmp, sp)
    except Exception:
        return

    _gc_stale()
    keeper = (not hwnd) or _dedupe_window(session_id, hwnd)   # at most one badge per window
    if keeper and not _alive_fresh(session_id):
        ap = _alive_path(session_id)
        try:                       # pre-mark alive so a rapid second touch won't double-spawn
            with open(ap, "w") as f:
                f.write(str(time.time() * 1000))
        except Exception:
            pass
        try:
            subprocess.Popen(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", BADGE_PS1,
                 "-StateFile", sp, "-AliveFile", ap, "-IdleMs", str(IDLE_MS)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=hc.CREATE_NO_WINDOW)
        except Exception:
            pass

    # Keep the global helpers alive: the window-tint accent and the Claude "new window" button.
    if cfg.get("window_tint", True):
        _ensure_singleton("window_tint", TINT_PS1)
    if cfg.get("button", True):
        _ensure_singleton("claude_button", BUTTON_PS1)


def _spawn_popup(title, body, color=None, hwnd=0, duration_ms=9000):
    """Draw our own always-on-top 'a session needs you' card (Windows only). Returns True if
    it was launched, so the caller can fall back to an OS toast off-Windows or on failure."""
    if os.name != "nt":
        return False
    try:
        r, g, b = ((list(color) + [0, 215, 80])[:3]) if color else (0, 215, 80)
    except Exception:
        r, g, b = 0, 215, 80
    try:
        subprocess.Popen(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", POPUP_PS1,
             "-Title", str(title or "Claude"), "-Body", str(body or "Waiting for you"),
             "-AccentR", str(int(r)), "-AccentG", str(int(g)), "-AccentB", str(int(b)),
             "-Hwnd", str(int(hwnd or 0)), "-DurationMs", str(int(duration_ms))],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=hc.CREATE_NO_WINDOW)
        return True
    except Exception:
        return False


def _status_pid(sid):
    return os.path.join(BADGE_DIR, f"status_{_sid8(sid)}.pid")


def _kill_status(sid):
    """Dismiss this chat's current top-right status card (if any)."""
    p = _status_pid(sid)
    try:
        pid = int(open(p).read().strip())
        subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True,
                       creationflags=hc.CREATE_NO_WINDOW)
    except Exception:
        pass
    try: os.remove(p)
    except Exception: pass


def _show_status(sid, cwd, working, detail=None):
    """Top-right card telling you what this chat is working on. One per chat: the new card
    replaces the chat's previous one. Sticky while working, brief when it finishes. `detail`
    (the user's prompt) makes the body specific - the actual task, not just "working"."""
    if os.name != "nt" or not hc.load_config().get("status_card", True):
        return
    st     = _read_state(sid)
    name   = st.get("label") or (os.path.basename(str(cwd).rstrip("/\\")) if cwd else "Claude")
    branch = st.get("branch") or ""
    if working:
        task = _short(detail, 90) if detail else ""      # the actual ask -> a specific description
        body = task or (f"working · {branch}" if branch and branch not in ("main", "master") else "working…")
        dur  = 900000                                    # stays up through the turn; replaced on stop
    else:
        body = "done"
        dur  = 6000                                      # brief "finished" card, then fades
    try:
        r, g, b = ((list(st.get("color")) + [0, 215, 80])[:3]) if st.get("color") else (0, 215, 80)
    except Exception:
        r, g, b = 0, 215, 80
    _kill_status(sid)
    pidf = _status_pid(sid)
    try:
        p = subprocess.Popen(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", POPUP_PS1,
             "-Title", str(name), "-Body", str(body),
             "-AccentR", str(int(r)), "-AccentG", str(int(g)), "-AccentB", str(int(b)),
             "-Hwnd", str(int(st.get("hwnd") or 0)), "-DurationMs", str(dur), "-PidFile", pidf],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=hc.CREATE_NO_WINDOW)
        with open(pidf, "w") as f:
            f.write(str(p.pid))
    except Exception:
        pass


def main():
    # Single hook entry point for every event. Maps the event to a badge state; captures
    # the window handle + refreshes the "working on" label at the moments the user is here.
    if os.environ.get("HAL_SUPPRESS"):     # inside a `claude -p` we launched to name a tab -> do nothing
        return
    try:
        data = json.loads(sys.stdin.read().lstrip("﻿"))
    except Exception:
        return
    ev  = data.get("hook_event_name", "")
    sid = data.get("session_id")
    cwd = data.get("cwd")
    tp  = data.get("transcript_path")
    if ev == "UserPromptSubmit":
        touch(sid, cwd, capture_hwnd=True, state="working", transcript_path=tp)
        _show_status(sid, cwd, working=True, detail=data.get("prompt"))   # top-right: the actual task
    elif ev == "SessionStart":
        touch(sid, cwd, capture_hwnd=True, state="done", transcript_path=tp)
    elif ev == "Stop":
        touch(sid, cwd, state="done", transcript_path=tp)   # response finished
        _show_status(sid, cwd, working=False)               # top-right: brief "done"
    elif ev == "Notification":
        was_waiting = _read_state(sid).get("state") == "waiting"
        touch(sid, cwd, state="waiting", reason=data.get("message"))   # awaiting your input/permission
        if not was_waiting:                                            # notify only on the transition
            cfg    = hc.load_config()
            st     = _read_state(sid)
            name   = st.get("label") or (os.path.basename(str(cwd).rstrip("/\\")) if cwd else "Claude")
            reason = st.get("reason") or "awaiting your input"
            shown  = False
            if cfg.get("popup", True):                                 # our own on-screen card (Windows)
                shown = _spawn_popup(name, f"Waiting for you — {reason}",
                                     st.get("color"), int(st.get("hwnd") or 0))
            if not shown and cfg.get("notify", True):                  # else fall back to an OS toast
                hal_notify.notify(f"Claude · {name}", f"Waiting for you — {reason}")
    elif ev in ("PreToolUse", "PostToolUse"):
        touch(sid, cwd, state="working")                    # keeps the badge/helpers fresh
    else:
        touch(sid, cwd)


if __name__ == "__main__":
    main()
