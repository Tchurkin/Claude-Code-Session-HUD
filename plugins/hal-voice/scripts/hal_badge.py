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
import glob, json, os, re, subprocess, sys, time, urllib.request
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hal_common as hc

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


def _foreground_hwnd():
    """The focused window ONLY if it's a VS Code window, so a chat's badge never binds to a
    browser/other app that happened to be focused when the async hook ran. 0 otherwise."""
    try:
        import ctypes
        u = ctypes.windll.user32
        h = u.GetForegroundWindow()
        if not h:
            return 0
        n = u.GetWindowTextLengthW(h)
        buf = ctypes.create_unicode_buffer(n + 1)
        u.GetWindowTextW(h, buf, n + 1)
        title = (buf.value or "").rstrip()
        return int(h) if title.endswith("Visual Studio Code") else 0
    except Exception:
        return 0


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


def _llm_topic(msgs):
    """1-3 word theme of the RECENT focus, via whichever LLM key is available (OpenAI, then
    Anthropic). Reflects current work so a chat that shifts topic re-labels itself."""
    convo = "\n".join(f"{r}: {t[:220]}" for r, t in msgs)[-4200:]
    prompt = ("Below are recent messages from an ongoing coding chat, oldest to newest:\n\n"
              + convo +
              "\n\nName what this chat is currently working on in 1 to 3 words (Title Case, no "
              "quotes or punctuation), reflecting the RECENT focus. Reply with ONLY the phrase.")
    key = _openai_key()
    if key:
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
    try:
        import anthropic
        m = anthropic.Anthropic(timeout=6.0).messages.create(
            model="claude-haiku-4-5-20251001", max_tokens=12,
            messages=[{"role": "user", "content": prompt}])
        t = m.content[0].text.strip().strip('."\'').strip()
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
    now = time.time() * 1000
    for f in glob.glob(os.path.join(BADGE_DIR, "*.json")):
        try:
            if now - float(json.load(open(f, encoding="utf-8")).get("ts", 0)) > IDLE_MS:
                sid8 = os.path.basename(f)[:-5]
                for p in (f, os.path.join(BADGE_DIR, sid8 + ".alive")):
                    try: os.remove(p)
                    except Exception: pass
        except Exception:
            pass


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
        if int(d.get("hwnd") or 0) == hwnd and (now - float(d.get("ts", 0))) < IDLE_MS:
            recent.append((float(d.get("ts", 0)), os.path.basename(f)[:-5], f))
    if len(recent) <= 1:
        return True
    keeper = max(recent)[1]                          # newest ts owns this window's badge
    for ts, sid8, f in recent:
        if sid8 != keeper:
            for p in (f, os.path.join(BADGE_DIR, sid8 + ".alive")):
                try: os.remove(p)
                except Exception: pass
    return _sid8(session_id) == keeper


def touch(session_id, cwd=None, capture_hwnd=False, state=None, transcript_path=None):
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
    r, g, b = hc.session_color(session_id)
    if capture_hwnd:
        fg = _foreground_hwnd()                       # 0 unless a VS Code window is focused
        hwnd = fg if fg else int(prev.get("hwnd") or 0)   # keep the last good one otherwise
    else:
        hwnd = int(prev.get("hwnd") or 0)

    label    = prev.get("label") or ""
    label_ts = float(prev.get("label_ts") or 0)
    if transcript_path and (not label or now - label_ts > TOPIC_EVERY):
        topic = _compute_topic(transcript_path)
        if topic:
            label, label_ts = topic, now
    if not label:
        label = os.path.basename(str(cwd).rstrip("/\\")) if cwd else ""

    st = state or prev.get("state") or "done"

    sp = _state_path(session_id)
    try:
        tmp = sp + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"ts": now, "color": [r, g, b], "label": label, "hwnd": hwnd,
                       "state": st, "label_ts": label_ts}, f)
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


def main():
    # Single hook entry point for every event. Maps the event to a badge state; captures
    # the window handle + refreshes the "working on" label at the moments the user is here.
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
    elif ev == "SessionStart":
        touch(sid, cwd, capture_hwnd=True, state="done", transcript_path=tp)
    elif ev == "Stop":
        touch(sid, cwd, state="done", transcript_path=tp)   # response finished
    elif ev == "Notification":
        touch(sid, cwd, state="waiting")                    # awaiting your input/permission
    elif ev in ("PreToolUse", "PostToolUse"):
        touch(sid, cwd, state="working")                    # keeps the badge/helpers fresh
    else:
        touch(sid, cwd)


if __name__ == "__main__":
    main()
