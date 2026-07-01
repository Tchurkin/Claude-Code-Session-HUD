#!/usr/bin/env python3
"""
Shared helpers for the session-HUD plugin: portable paths, config, and per-chat colors.
Imported by the single hook entry point - no heavy dependencies.

Machine-local state (config + scratch: badge state, PID/alive files) lives in
``~/.claude/hal_voice``.
"""
import hashlib, json, os

HOME        = os.path.expanduser("~")
DATA_DIR    = os.path.join(HOME, ".claude", "hal_voice")
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
PLUGIN_ROOT = os.environ.get("CLAUDE_PLUGIN_ROOT") or os.path.dirname(SCRIPTS_DIR)

CREATE_NO_WINDOW = 0x08000000   # Windows: don't flash a console window for child procs

_DEFAULTS = {
    "badge":       True,    # persistent per-chat color badge window (bottom-right)
    "window_tint": True,    # colored accent bar on the focused chat's VS Code window
    "button":      True,    # always-on-top button (new chat in a new window)
}

# Per-chat colors. Each session maps (by hashing its id) to one vivid accent, so several
# chats open at once are easy to tell apart.
SESSION_PALETTE = [
    (0, 215, 80),     # green
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
FAIL_COLOR = (240, 80, 70)   # error red (reserved for a future 'failed' badge state)


def session_color(session_id):
    """Stable vivid accent for a chat session (same chat -> same color every time)."""
    if not session_id:
        return SESSION_PALETTE[0]
    h = int(hashlib.md5(str(session_id).encode("utf-8")).hexdigest(), 16)
    return SESSION_PALETTE[h % len(SESSION_PALETTE)]


def ensure_data_dir():
    os.makedirs(DATA_DIR, exist_ok=True)
    return DATA_DIR


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
