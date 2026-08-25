#!/usr/bin/env python3
"""
Persistent per-chat color badge controller.

On activity a hook calls ``touch(...)``: it writes the badge's state (the chat's color,
a "what it's working on" label, the window to focus on click, and a working/done/awaiting
state) and, if no badge window is alive for that chat, spawns one (``badge.ps1``). The
window heartbeats ``<sid>.alive`` so we don't double-spawn. Also runnable directly as a hook
(reads session JSON on stdin) - used for SessionStart / UserPromptSubmit.

Hooks alone can't guarantee a tab for every open chat (an idle chat fires nothing), so which
tabs exist is owned by ``hal_sessions``: it reconciles the tab set against Claude Code's
registry of open sessions, and this module keeps each tab's *contents* current. Every hook
also runs a reconcile pass, so a missing tab is fixed within seconds either way.
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
METER_PS1   = os.path.join(hc.SCRIPTS_DIR, "hal_meter.ps1")
POPUP_PS1   = os.path.join(hc.SCRIPTS_DIR, "popup.ps1")
SESSIONS_PY = os.path.join(hc.SCRIPTS_DIR, "hal_sessions.py")
SLOTS_PATH  = os.path.join(hc.DATA_DIR, "slots.json")   # durable per-chat color memory (sid -> slot)
IDLE_MS     = 20 * 60 * 1000     # legacy (no session registry): dismiss after this much inactivity
TOPIC_EVERY = 30 * 60 * 1000     # how stale a name may get before a hook re-derives it. Long, on
                                 # purpose: a tab is named for its AREA of work, which barely
                                 # changes, and every prompt already re-checks it as a side effect
                                 # of summarising the turn. At 90s each Stop hook was launching a
                                 # fresh `claude -p` to re-answer a settled question - seconds of
                                 # CPU per turn, and names that drifted every few minutes.


def _sid8(session_id):
    s = "".join(ch for ch in str(session_id)[:8] if ch.isalnum())
    return s or "default"


def _state_path(sid): return os.path.join(BADGE_DIR, f"{_sid8(sid)}.json")
def _alive_path(sid): return os.path.join(BADGE_DIR, f"{_sid8(sid)}.alive")


def _read_beat(path):
    """(age_ms, pid) from an overlay's heartbeat file, or (None, 0) if there isn't one."""
    try:
        parts = open(path).read().strip().split()
        pid = int(parts[1]) if len(parts) > 1 else 0
        return (time.time() * 1000 - float(parts[0]), pid)
    except Exception:
        return (None, 0)


def _alive_fresh(sid):
    age, _ = _read_beat(_alive_path(sid))
    return age is not None and age < 4000


def _reap_wedged(path, stale_ms=9000):
    """An overlay whose heartbeat has gone stale while its process is still running is wedged - its
    frames are throwing. Kill it.

    This matters more than it looks: every overlay holds a named mutex, so a wedged one makes each
    replacement the supervisor spawns exit immediately at startup. The heartbeat stays stale, the
    spawn is retried forever, and the HUD is permanently and silently missing that piece - which is
    exactly how the window banners disappeared."""
    age, pid = _read_beat(path)
    if age is None or age < stale_ms or pid <= 0:
        return
    try:
        subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True,
                       creationflags=hc.CREATE_NO_WINDOW)
    except Exception:
        pass


def _read_state(sid):
    try:
        return json.load(open(_state_path(sid), encoding="utf-8"))
    except Exception:
        return {}


def _write_state(sid, obj):
    """Publish a chat's state atomically - the badge window re-reads this file constantly, so it
    must never see a half-written one."""
    try:
        os.makedirs(BADGE_DIR, exist_ok=True)
        sp = _state_path(sid)
        tmp = sp + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f)
        os.replace(tmp, sp)
        return True
    except Exception:
        return False


def retire(sid8):
    """Take a chat's tab down. Removing the state file IS the signal: the badge window notices it
    vanish and closes itself; its drawer/order markers go with it."""
    for p in (os.path.join(BADGE_DIR, sid8 + ".json"), os.path.join(BADGE_DIR, sid8 + ".alive"),
              os.path.join(BADGE_DIR, sid8 + ".stow"), os.path.join(BADGE_DIR, sid8 + ".ord")):
        try: os.remove(p)
        except Exception: pass


def update_state(sid, **fields):
    """Patch a few fields of a chat's state in place - for bookkeeping (its window title, when we
    last tried to name it) that shouldn't go through the full ``touch`` path."""
    st = _read_state(sid)
    if st:
        st.update(fields)
        _write_state(sid, st)


def mark_label_try(sid):
    """Note that we just tried to name this chat, so a lookup that comes back empty doesn't get
    retried in a tight loop."""
    st = _read_state(sid)
    if st:
        st["label_try"] = int(time.time() * 1000)
        _write_state(sid, st)


def _registry_mode():
    """True when Claude Code's session registry is readable. Then ``hal_sessions`` owns which tabs
    exist - one per open chat - and the older time/window-based cleanups here must stay out of its
    way (they were guesses at the same question, and they guessed wrong: they retired tabs that
    belonged to chats you still had open)."""
    try:
        import hal_sessions
        return hal_sessions.registry_available()
    except Exception:
        return False


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


def _find_chat_windows(cwd):
    """Every visible VS Code window whose title names this chat's project folder - a far more
    reliable 'which window is this chat in' than whatever was foreground when the async hook ran
    (which mis-binds when several windows are open).

    Sorted by handle, NOT by z-order: z-order reshuffles every time you switch windows, and when
    two chats share a project they'd trade windows underneath you on every pass."""
    proj = os.path.basename(str(cwd).rstrip("/\\")) if cwd else ""
    if not proj:
        return []
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
            return True

        u.EnumWindows(_cb, 0)
        return sorted(match)
    except Exception:
        return []


def _find_chat_window(cwd):
    """The VS Code window this chat most likely lives in (see ``_find_chat_windows``)."""
    w = _find_chat_windows(cwd)
    return w[0] if w else 0


VSC_SUFFIX = " - Visual Studio Code"
_AI_TITLE_RE = re.compile(r'"aiTitle"\s*:\s*"((?:[^"\\]|\\.)*)"')


def _ai_title(transcript_path):
    """The chat's OWN title - the one Claude Code gives the conversation, which VS Code then shows at
    the front of the window title. This is how we tell which window a chat is in: the project folder
    can't (several chats share a folder, and a multi-root workspace window never names it at all).
    Scanned straight out of the transcript: the tail first (a retitled chat writes a fresh one), then
    the head, because a chat is usually titled right after its opening exchange and a long-running
    one leaves that record megabytes behind."""
    if not transcript_path:
        return ""
    hits = []
    try:
        with open(transcript_path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 262144))
            hits = _AI_TITLE_RE.findall(f.read().decode("utf-8", "ignore"))
            if not hits and size > 262144:
                f.seek(0)
                hits = _AI_TITLE_RE.findall(f.read(262144).decode("utf-8", "ignore"))
    except Exception:
        return ""
    if not hits:
        return ""
    try:
        return json.loads('"%s"' % hits[-1])            # undo the JSON escaping
    except Exception:
        return hits[-1]


def _norm(s):
    """Compare-ready form of a title or tab label: VS Code truncates both, at different lengths and
    with an ellipsis, so the stem is all that can be compared."""
    return " ".join(str(s or "").split()).lower().rstrip("… .")


def _same_chat(a, b):
    """Do these two name the same chat? Either side may be a truncation of the other."""
    x, y = _norm(a), _norm(b)
    if not x or not y:
        return False
    if x == y:
        return True
    return min(len(x), len(y)) >= 8 and (x.startswith(y) or y.startswith(x))


def _window_chat_titles():
    """{hwnd: whatever each VS Code window is currently showing at the front of its title}.

    That leading segment is the active editor tab - for a Claude chat tab, the chat's own title,
    truncated with an ellipsis. Matching a chat against it binds the tab to the exact window,
    even when the window's folder is nameless or shared."""
    out = {}
    try:
        import ctypes
        from ctypes import wintypes
        u = ctypes.windll.user32

        @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
        def _cb(h, _l):
            if not u.IsWindowVisible(h):
                return True
            n = u.GetWindowTextLengthW(h)
            if n <= 0:
                return True
            buf = ctypes.create_unicode_buffer(n + 1)
            u.GetWindowTextW(h, buf, n + 1)
            t = (buf.value or "")
            if t.endswith(VSC_SUFFIX):
                head = t[:-len(VSC_SUFFIX)].split(" - ")[0]
                head = head.lstrip("●* ").rstrip()
                while head.endswith(("…", ".")):                # "Prepare sensor fusion an…"
                    head = head[:-1].rstrip()
                out[int(h)] = _norm(head)
            return True

        u.EnumWindows(_cb, 0)
    except Exception:
        pass
    return out


def _is_vscode_window(hwnd):
    """Still a live VS Code window? (Not 'still the right one' - see ``_hwnd_ok``.)"""
    try:
        return bool(hwnd) and ctypes_is_window(hwnd) and _window_title(hwnd).endswith(VSC_SUFFIX)
    except Exception:
        return False


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


def _hwnd_ok(hwnd, proj=None):
    """The stored handle is still a live VS Code window.

    Deliberately NOT "...whose title names this chat's project": a window opened on a multi-root
    workspace shows "Untitled (Workspace)", and a window showing a Claude chat leads with the chat's
    title - in both cases the folder never appears, and demanding it threw away a perfectly good
    binding (which then fell back to whichever window did happen to name the folder - the wrong one)."""
    return _is_vscode_window(hwnd)


def ctypes_is_window(hwnd):
    import ctypes
    return bool(ctypes.windll.user32.IsWindow(hwnd))


def _window_head(hwnd):
    """What a VS Code window is showing at the front of its title (its active tab), normalized."""
    t = _window_title(hwnd)
    if not t.endswith(VSC_SUFFIX):
        return ""
    head = t[:-len(VSC_SUFFIX)].split(" - ")[0].lstrip("●* ").rstrip()
    while head.endswith(("…", ".")):
        head = head[:-1].rstrip()
    return _norm(head)


def _capture_hwnd(cwd, chat_title=""):
    """This chat's window at a moment the user is demonstrably here (they just typed in it).

    The focused window only counts if it's plausibly *this* chat's: it's showing this chat by name,
    or at least names the project. Hooks run asynchronously, so by the time we look the user may
    have clicked into another window entirely - taking that at face value is how a tab ends up
    pointing somewhere it has never been. Returns 0 when nothing plausible is focused, which tells
    the caller to keep the binding it already had."""
    proj = os.path.basename(str(cwd).rstrip("/\\")) if cwd else ""
    fg = _foreground_hwnd()                       # focused VS Code window or 0
    if fg:
        head = _window_head(fg)
        mine = (head and chat_title and _norm(chat_title).startswith(head))
        if mine or (proj and proj in _window_title(fg)) or (not proj and not chat_title):
            return fg
    return _find_chat_window(cwd)


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
    (no API key, uses their subscription). Cached per process.

    On Windows an npm install puts THREE things on PATH - `claude` (a `#!/bin/sh` shim), `claude.cmd`
    and `claude.ps1` - and `shutil.which("claude")` hands back the sh shim, which Windows cannot
    execute. Launching it fails instantly and silently, so every tab quietly fell back to a scraped
    keyword name. Ask for real Windows executables by name instead, preferring the native binary."""
    global _CLAUDE_EXE, _CLAUDE_EXE_DONE
    if _CLAUDE_EXE_DONE:
        return _CLAUDE_EXE
    _CLAUDE_EXE_DONE = True
    exe = shutil.which("claude.exe" if os.name == "nt" else "claude")
    if exe:
        _CLAUDE_EXE = exe
        return exe
    cands = []                                             # the binary bundled with the editor extension
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
    if os.name == "nt":                                    # last resort: npm's shim, via cmd.exe
        for nm in ("claude.cmd", "claude.bat"):
            exe = shutil.which(nm)
            if exe:
                _CLAUDE_EXE = exe
                break
    return _CLAUDE_EXE


def _claude_cli_run(prompt):
    """Run a short completion through the user's Claude Code login (no API key). Headless, in a
    temp cwd (won't load project context), with HAL_SUPPRESS set so its own hooks no-op.

    The prompt goes in on stdin, not argv: it's multi-line and full of quotes, and if the CLI we
    found is a .cmd shim then argv gets re-parsed by cmd.exe on the way through. Pipes are forced to
    UTF-8 - they'd otherwise default to the ANSI codepage, and one Greek letter or arrow quoted from
    the conversation would raise mid-write and lose the name with no trace."""
    exe = _claude_exe()
    if not exe:
        return None
    try:
        env = dict(os.environ, HAL_SUPPRESS="1")
        r = subprocess.run(
            [exe, "-p", "--model", "claude-haiku-4-5-20251001"], input=prompt,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=60, env=env,
            cwd=tempfile.gettempdir(), creationflags=hc.CREATE_NO_WINDOW)
        t = (r.stdout or "").strip()
        if t:
            t = t.splitlines()[0].strip().strip('."\'').strip()
        if t and len(t) <= 60:
            return t
    except Exception:
        pass
    return None


def _llm_run(prompt, max_tokens=16):
    """Short completion via Claude: an Anthropic API key if set, else the local Claude Code CLI
    (your subscription, no key). OpenAI only if opted in (config `use_openai`). Returns a cleaned
    one-line string or None."""
    akey = _anthropic_key()
    if akey:
        try:
            import anthropic
            m = anthropic.Anthropic(api_key=akey, timeout=6.0).messages.create(
                model="claude-haiku-4-5-20251001", max_tokens=max_tokens,
                messages=[{"role": "user", "content": prompt}])
            t = m.content[0].text.strip().strip('."\'').strip()
            if t:
                return t
        except Exception:
            pass
    t = _claude_cli_run(prompt)            # your existing Claude Code login - no API key needed
    if t:
        return t
    if hc.load_config().get("use_openai", False) and _openai_key():   # ChatGPT: opt-in, off by default
        key = _openai_key()
        try:
            body = json.dumps({"model": "gpt-4o-mini", "max_tokens": max_tokens, "temperature": 0.3,
                               "messages": [{"role": "user", "content": prompt}]}).encode("utf-8")
            req = urllib.request.Request(
                "https://api.openai.com/v1/chat/completions", data=body,
                headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=7) as resp:
                d = json.loads(resp.read().decode("utf-8"))
            t = d["choices"][0]["message"]["content"].strip().strip('."\'').strip()
            if t:
                return t
        except Exception:
            pass
    return None


# What a tab name is, shared by both naming calls. A tab is glanced at, not read: it wants the AREA
# of work (stable, recognizable across weeks) rather than the task of the moment, which is what the
# status card is for. Asking for the momentary focus is what produced names like "Will Measure ALL".
_NAME_RULES = (
    "1 to 3 words, Title Case: the AREA OF WORK this chat is for. Broad and recognizable - a project "
    "or a discipline, the kind of label you would put on a folder: "
    '"College Apps", "Rocket Research", "Electronics Design", "Firmware Engineering", '
    '"Website Redesign", "Tax Prep". '
    "NOT the task of the moment, not a description of the last message, no verbs, no quotes, "
    "no punctuation.")


def _name_context(proj, siblings):
    """The two things that keep a name recognizable: the folder it's in (usually already the right
    label) and what its neighbours are called (so three chats in one repo don't share a name)."""
    s = f'Project folder: "{proj}"\n' if proj else ""
    if siblings:
        s += ("Other chats in this same folder are already named: "
              + ", ".join(f'"{x}"' for x in siblings)
              + ". Give this one a clearly different name for the different area it works on.\n")
    return s


def _llm_topic(msgs, proj="", siblings=()):
    """The tab name: a 1-3 word Title-Case label for this chat's area of work."""
    convo = "\n".join(f"{r}: {t[:220]}" for r, t in msgs)[-4200:]
    prompt = ("An ongoing Claude Code chat.\n"
              + _name_context(proj, siblings)
              + "\nRecent messages, oldest to newest:\n" + convo
              + "\n\nName this chat. " + _NAME_RULES
              + "\nThe folder name is a strong hint - use it when it fits.\n"
                "Reply with ONLY the phrase.")
    t = _llm_run(prompt, 12)
    return _short(t, 30) if t else None


def _focus_summary(msgs, prompt_text, current_label, proj="", siblings=()):
    """One LLM call for BOTH halves of what the HUD says about a chat: the tab's name (its area of
    work - broad, and meant to stay put) and the card's "what it's doing right now" (specific, and
    meant to change every turn). Returns {'label', 'phrase'} (either may be None), or None."""
    cur   = (current_label or "").strip()
    ptext = (prompt_text or "").strip()
    if not ptext and not msgs:
        return None
    convo = "\n".join(f"{r}: {t[:200]}" for r, t in (msgs or []))[-3500:]
    p = ("An ongoing Claude Code chat.\n"
         + _name_context(proj, siblings)
         + (f'Its current name is: "{cur}".\n' if cur else "")
         + (f"\nRecent messages:\n{convo}\n" if convo else "")
         + (f"\nThe user just asked:\n{ptext[:1200]}\n" if ptext else "")
         + "\nReply with exactly one line:  LABEL | DOING\n"
           "LABEL = " + _NAME_RULES + " "
         + (f'Keep "{cur}" unless this chat has moved to a genuinely different AREA of work - a new '
            "task within the same area is not a reason to rename it. " if cur else "")
         + "\nDOING = a 2 to 5 word lowercase gerund phrase for the task right now "
           '(e.g. "fixing sim bug", "adding servos to schematic", "testing popup visuals").\n'
           "Reply with ONLY:  LABEL | DOING")
    t = _llm_run(p, 24)
    if not t:
        return None
    line = t.splitlines()[0]
    lab, _, doing = line.partition("|")
    lab   = _short(lab.strip().strip('"\'.').strip(), 30)
    doing = _short(doing.strip().strip('"\'.').lower(), 44)
    return {"label": lab or None, "phrase": doing or None}


_PROJ_CRUFT = set("main master repo src copy temp final".split())   # only ever stripped from the END


def _proj_label(cwd):
    """A chat's project folder, tidied into a name: "college-apps" -> "College Apps",
    "Claude-Code-Session-HUD-main" -> "Claude Code Session HUD", "TVC PID Research" as-is.

    This is the fallback when Claude can't be reached, and it beats anything we can scrape from the
    transcript: a folder name is already at the altitude a tab wants - the area of work, recognizable
    at a glance - where a keyword theme reads like word salad ("Will Measure ALL")."""
    base = os.path.basename(str(cwd).rstrip("/\\")) if cwd else ""
    if not base:
        return ""
    words = [w for w in re.split(r"[-_.\s]+", base) if w]
    while len(words) > 1 and (words[-1].lower() in _PROJ_CRUFT              # "...-main" from a zip/clone
                              or re.fullmatch(r"v?\d+", words[-1].lower())):  # "...-v2"
        words.pop()
    out = " ".join(w if (w.isupper() or any(c.isupper() for c in w[1:])) else w.capitalize()
                   for w in words[:4])                # keep TVC / PID / HUD shouting; Title Case the rest
    return _short(out, 30)


def _stable_cwd(cwd, prev_cwd):
    """A chat's project folder, pinned to where it started.

    Hooks report the session's *current* directory, which moves the moment the chat cds somewhere -
    and a tab that renames itself "Scripts" because the work stepped into plugins/hal-voice/scripts
    is noise, not information. A subfolder of what we already had is that drift, so keep the parent;
    anything else is a genuinely different project and wins."""
    if not prev_cwd:
        return cwd
    if not cwd:
        return prev_cwd
    try:
        a = os.path.normcase(os.path.abspath(str(cwd)))
        b = os.path.normcase(os.path.abspath(str(prev_cwd)))
        if a == b or a.startswith(b.rstrip("\\/") + os.sep):
            return prev_cwd
    except Exception:
        pass
    return cwd


def _sibling_labels(session_id, cwd):
    """What the OTHER chats in this same project folder are already called. Several chats in one repo
    is the normal case here, and a tab strip reading "TVC PID Research" three times tells you nothing
    - so we hand the model its siblings' names and ask for one that's distinct."""
    proj = os.path.basename(str(cwd).rstrip("/\\")) if cwd else ""
    if not proj:
        return []
    ex, out = _sid8(session_id), []
    for f in glob.glob(os.path.join(BADGE_DIR, "*.json")):
        if os.path.basename(f)[:-5] == ex:
            continue
        try:
            d = json.load(open(f, encoding="utf-8"))
        except Exception:
            continue
        # Only chats that have a real name yet. A placeholder is the folder name, which every chat
        # in the folder is wearing - handing the model three copies of it as "the neighbours" is
        # noise, and would push it away from the one answer that might be right.
        if d.get("proj") == proj and d.get("label") and d.get("label_src") == "llm":
            out.append(str(d["label"]))
    return out[:6]


def _keyword_from_text(text):
    """Last-ditch fallback for a chat with no project folder at all: the top 2-3 meaningful words in
    `text`, in first-seen order."""
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


def _compute_topic(transcript_path, cwd=None, session_id=None):
    """(name, where it came from) - Claude reading the conversation, else the project folder (already
    the right kind of label), else - only for a chat with no folder - a keyword theme.

    The source is returned, not assumed: a fallback filed as if Claude had chosen it looks named and
    is never asked about again, which is how a tab gets stuck wearing a bad name."""
    proj = os.path.basename(str(cwd).rstrip("/\\")) if cwd else ""
    msgs = _recent_messages(transcript_path)
    if msgs:
        t = _llm_topic(msgs, proj, _sibling_labels(session_id, cwd) if session_id else ())
        if t:
            return t, "llm"
    return (_proj_label(cwd)
            or _keyword_from_text(" ".join(t for r, t in msgs if r == "user"))
            or _keyword_from_text(_first_user_message(transcript_path))
            or ""), "proj"


# ── lifecycle ──────────────────────────────────────────────────────────────────
def _ensure_singleton(name, ps1):
    """Keep one global helper window (window tint / Claude button) running. The .ps1 also
    guards itself with a named mutex, so this only avoids repeated spawn attempts."""
    ap = os.path.join(BADGE_DIR, name + ".alive")
    age, _ = _read_beat(ap)
    if age is not None and age < 4000:
        return
    _reap_wedged(ap)          # clear an incumbent that's stopped drawing but still owns the mutex
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


def _ensure_daemon():
    """Keep the session reconciler running - it is what guarantees a tab for every chat you have
    open, including ones that never fire a hook. Same shape as the other singletons: skip while its
    heartbeat is fresh, pre-mark so a burst of hooks doesn't spawn a crowd (and the daemon itself
    takes a named mutex, so a race still ends with exactly one)."""
    if os.name != "nt":
        return                                  # nothing renders tabs off Windows - don't leave a daemon
    ap = os.path.join(BADGE_DIR, "sessions_daemon.alive")
    try:
        if time.time() * 1000 - float(open(ap).read().strip().split()[0]) < 9000:
            return
    except Exception:
        pass
    try:
        with open(ap, "w") as f:
            f.write("%d 0" % int(time.time() * 1000))
    except Exception:
        pass
    try:
        subprocess.Popen([sys.executable or "python", SESSIONS_PY, "--daemon"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         stdin=subprocess.DEVNULL, creationflags=hc.CREATE_NO_WINDOW)
    except Exception:
        pass


def ensure_helpers(cfg=None):
    """Keep the always-on overlays up: the window-tint accent and the "new window" button.

    These used to be revived only from ``touch``, which the reconciler skips whenever no tab needs
    fixing - so a helper that died stayed dead for as long as the HUD had nothing else to do, and the
    window banners just quietly stopped appearing. The reconciler now checks them every pass; it's
    two file-age reads."""
    cfg = cfg or hc.load_config()
    if cfg.get("window_tint", True):
        _ensure_singleton("window_tint", TINT_PS1)
    if cfg.get("usage_meter", True):
        _ensure_singleton("usage_meter", METER_PS1)


def _gc_stale():
    # Legacy path only (no session registry to ask what's open): reap state whose badge is NOT
    # running and has since gone quiet. With a registry, hal_sessions retires tabs by whether the
    # chat is still open - never by how long it's been since it last did something.
    now = time.time() * 1000
    for f in glob.glob(os.path.join(BADGE_DIR, "*.json")):
        sid8 = os.path.basename(f)[:-5]
        if _alive_fresh(sid8):
            continue
        try:
            if now - float(json.load(open(f, encoding="utf-8")).get("ts", 0)) > IDLE_MS:
                retire(sid8)
        except Exception:
            pass


def _active_slots(exclude_sid):
    """Color slots held by other, currently-relevant sessions. With the registry driving lifecycle
    a state file exists exactly while its chat is open, so every other one's slot is taken;
    without it, a slot counts as taken if that session's badge is alive OR it was touched recently
    - NOT alive alone, so a live session whose heartbeat briefly lapses (e.g. right after a reload)
    doesn't look 'free' and get its color handed to a new chat. Being inclusive here is the safe
    side: worst case a new chat picks a higher slot; the unsafe side is two chats sharing a color."""
    now  = time.time() * 1000
    ex   = _sid8(exclude_sid)
    reg  = _registry_mode()
    used = set()
    for f in glob.glob(os.path.join(BADGE_DIR, "*.json")):
        sid8 = os.path.basename(f)[:-5]
        if sid8 == ex:
            continue
        try:
            d = json.load(open(f, encoding="utf-8"))
        except Exception:
            continue
        if not (reg or _alive_fresh(sid8) or (now - float(d.get("ts", 0))) < IDLE_MS):
            continue
        s = d.get("slot")
        if s is not None:
            try: used.add(int(s))
            except Exception: pass
    return used


def _load_slots():
    try:
        return json.load(open(SLOTS_PATH, encoding="utf-8"))
    except Exception:
        return {}


def _save_slots(m):
    try:
        os.makedirs(os.path.dirname(SLOTS_PATH), exist_ok=True)
        tmp = SLOTS_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(m, f)
        os.replace(tmp, SLOTS_PATH)
    except Exception:
        pass


def _session_slot(session_id, used, prev_slot=None):
    """The chat's stable color slot. Prefers the color it's had before - remembered durably (so it
    survives despawn / respawn / state GC), falling back to the live state's slot - and keeps it
    unless another *currently-open* chat holds that color, in which case it moves to the lowest free
    one. So a chat's color is stable identity; it only ever changes to break a genuine clash."""
    sid8 = _sid8(session_id)
    m = _load_slots()
    entry = m.get(sid8)
    remembered = entry.get("slot") if isinstance(entry, dict) else entry
    if remembered is None:
        remembered = prev_slot
    slot = remembered
    if slot is None or int(slot) in used:              # never seen, or its color is taken by a live chat
        slot = 0
        while slot in used:
            slot += 1
    slot = int(slot)
    if not isinstance(entry, dict) or entry.get("slot") != slot:   # persist only on change (low churn)
        m[sid8] = {"slot": slot, "ts": int(time.time() * 1000)}
        if len(m) > 300:                               # bound the registry: drop the oldest entries
            old = sorted(m, key=lambda k: m[k].get("ts", 0) if isinstance(m[k], dict) else 0)
            for k in old[:len(m) - 300]:
                m.pop(k, None)
        _save_slots(m)
    return slot


def _dedupe_window(session_id, hwnd):
    """Legacy path only (no session registry). One badge per VS Code window: keep the
    most-recently-active session for a given window and retire the rest. This is a guess - two
    chats really can live in one window - so where the registry can tell us what's open,
    hal_sessions dedupes by session id instead and every open chat keeps its tab. Returns True if
    `session_id` is the keeper."""
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
            retire(sid8)
    return _sid8(session_id) == keeper


def touch(session_id, cwd=None, capture_hwnd=False, state=None, transcript_path=None, reason=None,
          name=None, hwnd=None, label_src=None, keep_ts=False):
    """Refresh this chat's badge state and ensure its window is running.

    state: 'working' | 'done' | 'waiting' (drives the on-badge indicator).
    transcript_path: when given (SessionStart/UserPromptSubmit), refresh the label.
    capture_hwnd: record which window to focus on click (only at moments the user is here).
    hwnd/label_src/keep_ts: used by the reconciler, which already knows which window this chat
    belongs to, whether its name is a placeholder, and that adopting a tab is not chat activity."""
    cfg = hc.load_config()
    if not session_id or not cfg.get("badge", True):
        return
    os.makedirs(BADGE_DIR, exist_ok=True)
    now  = int(time.time() * 1000)
    prev = _read_state(session_id)
    # Fast path. PreToolUse/PostToolUse fire on every Bash call and every edit, and nearly all of
    # them are saying "still working" about a chat that is already marked working, with a live tab.
    # The full path below re-reads every state file and enumerates windows; skip it when there is
    # demonstrably nothing to change.
    if (state and not capture_hwnd and not transcript_path and not name and not reason
            and hwnd is None and prev.get("state") == state
            and (now - float(prev.get("ts") or 0)) < 2000 and _alive_fresh(session_id)):
        _ensure_daemon()
        return
    cwd  = _stable_cwd(cwd, prev.get("cwd"))         # ignore the chat cd-ing around inside its project
    used = _active_slots(session_id)                 # colors currently held by other open chats
    slot = _session_slot(session_id, used, prev.get("slot"))   # this chat's stable, remembered color
    r, g, b = hc.slot_color(slot)
    proj   = os.path.basename(str(cwd).rstrip("/\\")) if cwd else ""
    prev_h = int(prev.get("hwnd") or 0)
    if hwnd:
        hwnd = int(hwnd)                             # caller resolved it (reconciler)
    elif capture_hwnd:                               # user is here -> prefer the focused window,
        hwnd = _capture_hwnd(cwd, prev.get("title")) or prev_h    # but only if it's plausibly ours
    elif _hwnd_ok(prev_h, proj):
        hwnd = prev_h                                # stored handle still valid -> keep it
    else:
        hwnd = _find_chat_window(cwd) or prev_h      # stale/wrong -> re-find by project title

    label    = prev.get("label") or ""
    label_ts = float(prev.get("label_ts") or 0)
    src      = prev.get("label_src") or ""
    if name:                                          # caller supplied a fresh, focus-aware name
        label, label_ts, src = name, now, (label_src or "llm")
    elif transcript_path and (not label or now - label_ts > TOPIC_EVERY):
        topic, tsrc = _compute_topic(transcript_path, cwd, session_id)
        if topic:
            label, label_ts, src = topic, now, tsrc
    if not label:
        label = _proj_label(cwd)
        if label:      # a placeholder still counts as named, so we don't re-ask Claude every touch
            label_ts, src = now, (src or "proj")

    st = state or prev.get("state") or "done"
    branch = _git_branch(cwd) if capture_hwnd else (prev.get("branch") or "")   # feature/worktree branch
    reason_val = _short(reason, 30) if reason else (prev.get("reason") or "")    # what it's waiting on
    present_ts = now if capture_hwnd else float(prev.get("present_ts") or 0)      # last time the user was here
    # ts is "when this chat last did something" - adopting or repairing a tab isn't that, and
    # overwriting it would make an idle chat look busy (and delay retiring it once it closes).
    ts = float(prev.get("ts") or now) if (keep_ts and prev) else now
    out = dict(prev)                  # keep bookkeeping the reconciler owns (window title, showing)
    out.update({
        "ts": ts, "color": [r, g, b], "slot": slot, "label": label, "hwnd": hwnd,
        "state": st, "label_ts": label_ts, "branch": branch, "reason": reason_val,
        "present_ts": present_ts, "proj": proj, "cwd": (str(cwd) if cwd else ""),
        "label_src": src, "label_try": prev.get("label_try") or 0})
    out.pop("gone", None)             # this chat is demonstrably alive
    out.pop("ended_ts", None)
    out.pop("usage", None)            # the old per-chat context-fill number; the meter is plan-wide now
    if not _write_state(session_id, out):
        return

    if _registry_mode():
        _ensure_daemon()        # the reconciler owns which tabs exist; keep it running
        keeper = True           # every open chat gets a tab, even two in the same window
    else:
        _gc_stale()
        keeper = (not hwnd) or _dedupe_window(session_id, hwnd)   # at most one badge per window
    if keeper and not _alive_fresh(session_id):
        sp = _state_path(session_id)
        ap = _alive_path(session_id)
        _reap_wedged(ap)      # a wedged badge holds this chat's mutex; a new one would just exit
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

    ensure_helpers(cfg)


def _chat_ref(sid):
    """How to name this chat to the VS Code extension when a card is clicked: its editor tab's exact
    label if the extension has told us one, else the chat's own title."""
    st = _read_state(sid)
    return str(st.get("tab") or st.get("title") or "")


def _spawn_popup(title, body, color=None, hwnd=0, duration_ms=9000, chat="", sid=""):
    """Draw our own always-on-top 'a session needs you' card (Windows only). Returns True if
    it was launched, so the caller can fall back to an OS toast off-Windows or on failure."""
    if os.name != "nt":
        return False
    try:
        r, g, b = ((list(color) + [0, 215, 80])[:3]) if color else (0, 215, 80)
    except Exception:
        r, g, b = 0, 215, 80
    args = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", POPUP_PS1,
            "-Title", str(title or "Claude"), "-Body", str(body or "Waiting for you"),
            "-AccentR", str(int(r)), "-AccentG", str(int(g)), "-AccentB", str(int(b)),
            "-Hwnd", str(int(hwnd or 0)), "-DurationMs", str(int(duration_ms)),
            "-Chat", str(chat or ""), "-Sid", _sid8(sid) if sid else ""]
    if sid:
        _kill_status(sid)                       # one card per chat: this one replaces the last
        args += ["-PidFile", _status_pid(sid)]
    try:
        p = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             creationflags=hc.CREATE_NO_WINDOW)
        if sid:
            try:
                with open(_status_pid(sid), "w") as f:
                    f.write(str(p.pid))         # the card overwrites this with its own pid + slot
            except Exception:
                pass
        return True
    except Exception:
        return False


def _question_text(tool_input):
    """The gist of what a chat is asking, for the tab and the card. AskUserQuestion carries a short
    header per question - exactly the label we want - with the full question as the fallback."""
    try:
        qs = (tool_input or {}).get("questions") or []
        if qs:
            q = qs[0]
            return _short(q.get("header") or q.get("question") or "", 34)
        if (tool_input or {}).get("plan"):
            return "ready to start?"
    except Exception:
        pass
    return ""


_WAV_CACHE = {}


def _chime_wav(freqs, ms=110, vol=0.45, rate=22050):
    """A little PCM chime, built in memory. Each tone is faded in and out so it doesn't click."""
    import math, struct
    frames = bytearray()
    for f in freqs:
        n = int(rate * ms / 1000)
        for i in range(n):
            env = min(1.0, i / 220.0, (n - i) / 220.0)
            frames += struct.pack("<h", int(32767 * vol * env * math.sin(2 * math.pi * f * i / rate)))
    return (b"RIFF" + struct.pack("<I", 36 + len(frames)) + b"WAVEfmt "
            + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16)
            + b"data" + struct.pack("<I", len(frames)) + bytes(frames))


def _beep(kind="attention"):
    """A short chime for the moments that actually want you: a chat asking you a question, one
    blocked on a permission, one that has finished while you were elsewhere.

    Played as a real waveform through the normal audio device rather than winsound.Beep - Beep
    drives the legacy system-beep channel, which on plenty of machines is quiet or silent however
    loud the speakers are, and it blocks the caller for the length of the tone. Two tones rising
    means "answer me", one lower note means "finished". `sound: false` in the config turns it off."""
    if os.name != "nt" or not hc.load_config().get("sound", True):
        return
    try:
        import winsound
        if kind not in _WAV_CACHE:
            _WAV_CACHE[kind] = (_chime_wav([660], 130, 0.32) if kind == "done"
                                else _chime_wav([880, 1245], 95, 0.5))
        winsound.PlaySound(_WAV_CACHE[kind], winsound.SND_MEMORY | winsound.SND_ASYNC)
    except Exception:
        try:
            import winsound
            winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
        except Exception:
            pass


def _fg_hwnd():
    """The raw foreground window handle (to tell if a chat's own window is the one you're on)."""
    try:
        import ctypes
        return int(ctypes.windll.user32.GetForegroundWindow())
    except Exception:
        return 0


def _status_pid(sid):
    return os.path.join(BADGE_DIR, f"status_{_sid8(sid)}.pid")


def _kill_status(sid):
    """Dismiss this chat's current top-right card, whichever kind it is - one card per chat, so a
    new one about the same chat replaces the old rather than stacking a second opinion beside it.

    The card records its stacking slot next to its pid; drop that too, or the replacement stacks
    below a ghost and leaves a gap where the old card was."""
    p = _status_pid(sid)
    try:
        parts = open(p).read().strip().split(None, 1)
        subprocess.run(["taskkill", "/F", "/PID", str(int(parts[0]))], capture_output=True,
                       creationflags=hc.CREATE_NO_WINDOW)
        if len(parts) > 1 and parts[1]:
            try: os.remove(parts[1])
            except Exception: pass
    except Exception:
        pass
    try: os.remove(p)
    except Exception: pass


def _show_status(sid, cwd, working, detail=None, waiting=False, asking=False):
    """Top-right card telling you what this chat is working on. One per chat: the new card
    replaces the chat's previous one. Sticky while working, brief when it finishes, or a
    persistent 'waiting for your response' when a background chat finishes and needs you."""
    if os.name != "nt" or not hc.load_config().get("status_card", True):
        return
    st     = _read_state(sid)
    name   = st.get("label") or (os.path.basename(str(cwd).rstrip("/\\")) if cwd else "Claude")
    branch = st.get("branch") or ""
    # Holds, after which the card fades out over FADE_MS. These used to be 15 minutes for anything
    # that wasn't "done", which is why cards appeared never to fade at all - they simply sat there.
    # The tab is the durable signal; a card is a glance, so it should leave on its own.
    if asking:
        body = detail or "asking you a question"
        dur  = 40000                                     # a question deserves to sit there a while
    elif waiting:
        body = "waiting for your response"
        dur  = 40000                                     # finished and wants you: linger, then fade
    elif working:
        body = detail or (f"working · {branch}" if branch and branch not in ("main", "master") else "working…")
        dur  = 12000                                     # ambient "what it's doing"; the tab carries it after
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
             "-Hwnd", str(int(st.get("hwnd") or 0)), "-DurationMs", str(dur), "-PidFile", pidf,
             "-Chat", _chat_ref(sid), "-Sid", _sid8(sid)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=hc.CREATE_NO_WINDOW)
        with open(pidf, "w") as f:
            f.write(str(p.pid))
    except Exception:
        pass


def _mark_ended(sid):
    """This chat has closed. Flag it so the reconciler can drop its tab as soon as the process is
    actually gone, instead of waiting out the usual grace period."""
    st = _read_state(sid)
    if st:
        st["ended_ts"] = int(time.time() * 1000)
        _write_state(sid, st)


RECONCILE_MARK = os.path.join(BADGE_DIR, "reconcile.beat")


def _reconcile(force=False):
    """Every hook is also a chance to fix the whole HUD, not just this chat's tab: adopt chats that
    have none, respawn windows that died, drop tabs whose chat has closed.

    Throttled against the daemon's own pass, which stamps the same marker. A reconcile walks every
    window on the desktop, so doing one per Bash call - on top of the daemon doing one every few
    seconds anyway - was work nobody asked for. SessionStart/SessionEnd skip the throttle: those are
    exactly the moments the tab set has changed."""
    try:
        if not force:
            age, _ = _read_beat(RECONCILE_MARK)
            if age is not None and age < 4500:
                return
        import hal_sessions
        hal_sessions.reconcile()
    except Exception:
        pass


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--beep":      # `hal_badge.py --beep [asking|done]`
        _beep(sys.argv[2] if len(sys.argv) > 2 else "attention")
        time.sleep(0.6)                                     # let the async sound finish before we exit
        return
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

    cfg = hc.load_config()
    if not cfg.get("enabled", True):                  # HUD switched off (via the VS Code status-bar
        return                                        # extension or config): draw nothing, overlays self-close

    if ev == "SessionEnd":
        # /clear ends a conversation but not the chat - the window and its process stay, so the tab
        # stays too. Anything else means it's really going; the reconciler confirms and takes it down.
        if (data.get("reason") or "") != "clear":
            _mark_ended(sid)
        _reconcile(force=True)
        return
    if ev == "UserPromptSubmit":
        # Show it working FIRST: naming asks Claude and can take tens of seconds, and the tab must
        # not sit there looking idle for that long just because we're deciding what to call it.
        touch(sid, cwd, capture_hwnd=True, state="working")
        fu     = _focus_summary(_recent_messages(tp) if tp else [], data.get("prompt"),
                                _read_state(sid).get("label"),     # re-check the name + summarize the task
                                os.path.basename(str(cwd).rstrip("/\\")) if cwd else "",
                                _sibling_labels(sid, cwd))
        name   = (fu or {}).get("label")
        phrase = (fu or {}).get("phrase")
        touch(sid, cwd, capture_hwnd=True, state="working", transcript_path=tp, name=name)
        _show_status(sid, cwd, working=True, detail=phrase)        # short "what it's doing"
    elif ev == "SessionStart":
        touch(sid, cwd, capture_hwnd=True, state="done", transcript_path=tp)
        _reconcile(force=True)   # a new chat needs its tab now, not on the next pass
    elif ev == "Stop":
        touch(sid, cwd, state="done", transcript_path=tp)   # response finished
        hw = int(_read_state(sid).get("hwnd") or 0)
        if hw and hw == _fg_hwnd():
            _show_status(sid, cwd, working=False)                    # you're watching -> brief "done"
        else:
            _show_status(sid, cwd, working=False, waiting=True)      # background chat -> waiting on your reply
            _beep("done")                                            # a softer note: finished, not blocked
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
                                     st.get("color"), int(st.get("hwnd") or 0),
                                     chat=_chat_ref(sid), sid=sid)
            if not shown and cfg.get("notify", True):                  # else fall back to an OS toast
                hal_notify.notify(f"Claude · {name}", f"Waiting for you — {reason}")
            _beep("attention")
    elif ev == "PreToolUse" and data.get("tool_name") in ("AskUserQuestion", "ExitPlanMode"):
        # Claude is putting a question to you - a different thing from "busy" and from "blocked on a
        # permission", and the one state where the chat is waiting on your judgement. Its own icon on
        # the tab, its own card, its own chime.
        q = _question_text(data.get("tool_input"))
        was = _read_state(sid).get("state")
        touch(sid, cwd, state="asking", reason=q or "asking you a question")
        if was != "asking":
            _show_status(sid, cwd, working=False, asking=True, detail=q)
            _beep("attention")
    elif ev in ("PreToolUse", "PostToolUse"):
        touch(sid, cwd, state="working")                    # keeps the badge/helpers fresh
    else:
        touch(sid, cwd)

    _reconcile()      # ...and while we're here, make sure every other open chat has its tab too


if __name__ == "__main__":
    main()
