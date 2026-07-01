#!/usr/bin/env python3
"""
Shared helpers for the HAL-voice plugin: portable paths, config, the voice pool,
audio playback, and clip durations.

Design notes
------------
* This module is imported by BOTH the lightweight hooks (run by the user's default
  ``python``, which has no torch / f5-tts) AND the heavy synthesis scripts (run by the
  configured ``tts_python`` venv).  So it must NOT import torch / f5_tts / imageio at
  module load - those imports are guarded inside the functions that need them.
* All machine-local state (config + runtime scratch) lives in ``~/.claude/hal_voice``.
* The writable / git-synced pool, the voice reference, and the F5 venv all live OUTSIDE
  the installed (read-only, cached) plugin and are located via config.  When unconfigured
  we fall back to the copies bundled inside the plugin so a fresh marketplace install
  still plays lines out of the box.
"""
import hashlib, json, os, subprocess, sys

# ── locations ────────────────────────────────────────────────────────────────
HOME        = os.path.expanduser("~")
DATA_DIR    = os.path.join(HOME, ".claude", "hal_voice")          # config + scratch
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")
ACTION_FILE = os.path.join(DATA_DIR, "last_action.json")
DAEMON_PID  = os.path.join(DATA_DIR, "tts_daemon.pid")

AUTO_PREFIX = "hal_auto_"          # filenames for hook-synthesized (non-base) lines

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
# ${CLAUDE_PLUGIN_ROOT} when a hook sets it; otherwise infer from this file's location.
PLUGIN_ROOT = os.environ.get("CLAUDE_PLUGIN_ROOT") or os.path.dirname(SCRIPTS_DIR)
BUNDLED_POOL = os.path.join(PLUGIN_ROOT, "hal_pool")
BUNDLED_REF  = os.path.join(PLUGIN_ROOT, "reference")

POPUP_PS1   = os.path.join(SCRIPTS_DIR, "popup.ps1")
STATUS_PS1  = os.path.join(SCRIPTS_DIR, "status_popup.ps1")
PLAY_PS1    = os.path.join(SCRIPTS_DIR, "play_audio.ps1")

CREATE_NO_WINDOW = 0x08000000   # Windows: don't flash a console window for child procs

_DEFAULTS = {
    "user_name":       "Braxton",
    "pool_dir":        None,     # writable/synced pool; falls back to BUNDLED_POOL
    "reference_dir":   None,     # falls back to BUNDLED_REF
    "tts_python":      None,     # venv python that has f5-tts (enables live synth)
    "pool_repo":       None,     # git repo root for cross-device sync
    "gpu":             False,    # set by hal_setup; CPU F5 is far too slow to be "live"
    "live_synth":      True,     # master switch for attempting live synthesis
    "muted":           False,    # /hal-mute: silence HAL (voice + popups) without uninstalling
    "badge":           True,     # persistent per-chat color badge window (bottom-right)
    "window_tint":     True,     # colored accent bar on the focused chat's VS Code window
    "button":          True,     # always-on-top Claude spark button (new chat in new window)
    "synth_budget_ms": 6000,     # how long the announcer waits for a live line
    "daemon_port":     53117,    # localhost port for the warm F5 synth daemon
    "daemon_idle_s":   900,      # daemon self-exits after this many idle seconds
}

# Line "kind": which moment a pool line is for. Absent/"done" = a completed task (the
# original behaviour); "fail" = something went wrong; "wait" = Claude is awaiting input.
KINDS = ("done", "fail", "wait")

# Per-chat popup colors. Each Claude session maps (by hashing its id) to one vivid accent,
# so two chats open at once are easy to tell apart. All chosen to read well on the popups'
# near-black fill. FAIL_COLOR overrides the chat color so failures always look like failures.
SESSION_PALETTE = [
    (0, 215, 80),     # green (the original)
    (0, 200, 255),    # cyan
    (255, 176, 0),    # amber
    (235, 70, 200),   # magenta
    (170, 110, 255),  # purple
    (70, 150, 255),   # blue
    (0, 205, 170),    # teal
    (255, 120, 40),   # orange
    (140, 220, 0),    # lime
    (255, 105, 160),  # pink
    (90, 190, 255),   # sky
    (210, 190, 40),   # gold
    (120, 230, 160),  # mint
    (200, 120, 255),  # violet
]
FAIL_COLOR = (240, 80, 70)   # error red


def session_color(session_id):
    """Stable vivid accent for a chat session (same chat -> same color every time)."""
    if not session_id:
        return SESSION_PALETTE[0]
    h = int(hashlib.md5(str(session_id).encode("utf-8")).hexdigest(), 16)
    return SESSION_PALETTE[h % len(SESSION_PALETTE)]


def daemon_addr(cfg=None):
    cfg = cfg or load_config()
    return ("127.0.0.1", int(cfg.get("daemon_port", 53117)))


def ensure_data_dir():
    os.makedirs(DATA_DIR, exist_ok=True)
    return DATA_DIR


# ── config ───────────────────────────────────────────────────────────────────
def load_config():
    cfg = dict(_DEFAULTS)
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg.update(json.load(f))
    except Exception:
        pass
    return cfg


def save_config(cfg):
    ensure_data_dir()
    merged = dict(_DEFAULTS)
    merged.update(cfg)
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(merged, f, indent=2)
    os.replace(tmp, CONFIG_PATH)
    return merged


def pool_dir(cfg=None):
    cfg = cfg or load_config()
    p = cfg.get("pool_dir")
    if p and os.path.isdir(p):
        return p
    return BUNDLED_POOL


def reference_dir(cfg=None):
    cfg = cfg or load_config()
    p = cfg.get("reference_dir")
    if p and os.path.isdir(p):
        return p
    return BUNDLED_REF


def is_muted(cfg=None):
    """True when the user has silenced HAL via /hal-mute (config `muted`)."""
    cfg = cfg or load_config()
    return bool(cfg.get("muted"))


def can_synth_live(cfg=None):
    """True only when this machine can realistically synthesize a line fast enough to
    be played in-line (configured f5 venv + a GPU + the master switch on)."""
    cfg = cfg or load_config()
    if not cfg.get("live_synth", True):
        return False
    tp = cfg.get("tts_python")
    return bool(tp and os.path.exists(tp) and cfg.get("gpu"))


# ── the pool ─────────────────────────────────────────────────────────────────
def manifest_path(pdir=None):
    return os.path.join(pdir or pool_dir(), "manifest.json")


def load_pool(pdir=None):
    """Return manifest entries whose mp3 actually exists, newest-last."""
    pdir = pdir or pool_dir()
    try:
        entries = json.loads(open(manifest_path(pdir), encoding="utf-8").read())
    except Exception:
        return []
    return [e for e in entries if os.path.exists(os.path.join(pdir, e.get("file", "")))]


def save_pool(entries, pdir=None):
    pdir = pdir or pool_dir()
    os.makedirs(pdir, exist_ok=True)
    tmp = manifest_path(pdir) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(entries, f, indent=2)
    os.replace(tmp, manifest_path(pdir))


def entry_duration_ms(entry, default=6000):
    try:
        v = int(entry.get("dur_ms"))
        return v if v > 0 else default
    except Exception:
        return default


def entry_kind(entry):
    """Normalized line kind; a missing/unknown kind is treated as a completion line."""
    k = (entry.get("kind") or "done").strip().lower()
    return k if k in KINDS else "done"


def pool_by_kind(entries, kind):
    """Subset of the pool for a given moment ('done' / 'fail' / 'wait')."""
    kind = (kind or "done").lower()
    return [e for e in entries if entry_kind(e) == kind]


def auto_filename(text):
    """Deterministic filename for a synthesized line, so callers can predict the path."""
    h = hashlib.md5(text.strip().encode("utf-8")).hexdigest()[:10]
    return f"{AUTO_PREFIX}{h}.mp3"


def find_entry(text, pdir=None):
    t = text.strip().lower()
    for e in load_pool(pdir):
        if e.get("text", "").strip().lower() == t:
            return e
    return None


def append_pool_entry(text, file, dur_ms, pdir=None, kind=None):
    """Idempotently add a line to the manifest (atomic re-read + replace; race-tolerant)."""
    pdir = pdir or pool_dir()
    try:
        cur = json.loads(open(manifest_path(pdir), encoding="utf-8").read())
    except Exception:
        cur = []
    t = text.strip().lower()
    if any(e.get("file") == file or e.get("text", "").strip().lower() == t for e in cur):
        return False
    entry = {"file": file, "text": text.strip(), "dur_ms": int(dur_ms)}
    if kind and str(kind).lower() in ("fail", "wait"):   # 'done' is the implicit default
        entry["kind"] = str(kind).lower()
    cur.append(entry)
    save_pool(cur, pdir)
    return True


# ── ffmpeg (only needed by the synth/build scripts, which run in the venv) ─────
def find_ffmpeg():
    import shutil
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


# ── audio + popups ───────────────────────────────────────────────────────────
def play_audio(path, wait=True, timeout=30):
    """Play an mp3/wav with the dependency-free MediaPlayer helper (no ffplay needed)."""
    abspath = os.path.abspath(path)
    cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
           "-File", PLAY_PS1, "-Path", abspath]
    try:
        if wait:
            subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=timeout, creationflags=CREATE_NO_WINDOW)
        else:
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             creationflags=CREATE_NO_WINDOW)
    except Exception:
        pass


def _accent_args(accent):
    r, g, b = accent or SESSION_PALETTE[0]
    return ["-AccentR", str(int(r)), "-AccentG", str(int(g)), "-AccentB", str(int(b))]


def show_completion_popup(text, duration_ms, accent=None):
    try:
        subprocess.Popen(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-File", POPUP_PS1, "-Text", text, "-DurationMs", str(int(duration_ms))]
            + _accent_args(accent),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW)
    except Exception:
        pass


def status_pid_path(session_id=None):
    """Per-chat status-popup PID file, so a chat replaces only ITS OWN status popup and
    leaves other chats' popups alone (they stack instead of clobbering each other)."""
    sid = "".join(ch for ch in str(session_id)[:8] if ch.isalnum()) if session_id else ""
    return os.path.join(DATA_DIR, f"status_popup_{sid or 'default'}.pid")


def show_status_popup(text, loading=True, duration_ms=300000, accent=None, session_id=None):
    """Replace this chat's visible status popup with a new one; record its PID to kill later."""
    pidfile = status_pid_path(session_id)
    kill_status_popup(session_id)
    ensure_data_dir()
    args = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-NonInteractive",
            "-File", STATUS_PS1, "-Text", text, "-DurationMs", str(int(duration_ms)),
            "-PidFile", pidfile] + _accent_args(accent)
    if loading:
        args.append("-Loading")
    try:
        p = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             creationflags=CREATE_NO_WINDOW)
        with open(pidfile, "w") as f:
            f.write(str(p.pid))
    except Exception:
        pass


def kill_status_popup(session_id=None):
    pidfile = status_pid_path(session_id)
    try:
        if not os.path.exists(pidfile):
            return
        pid = int(open(pidfile).read().strip())
        subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                       capture_output=True, creationflags=CREATE_NO_WINDOW)
    except Exception:
        pass
    finally:
        try: os.remove(pidfile)
        except Exception: pass


def windows_tts(text):
    """Absolute last resort (no pool, no synth): speak via the built-in Windows voice."""
    safe = text.replace("'", "")
    ps = ("Add-Type -AssemblyName System.Speech; "
          "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; $s.Rate = -2; "
          f"$s.Speak('{safe}'); $s.Dispose()")
    try:
        subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                       capture_output=True, timeout=15, creationflags=CREATE_NO_WINDOW)
    except Exception:
        pass
