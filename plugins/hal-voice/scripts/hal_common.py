#!/usr/bin/env python3
"""
Shared helpers for the session-HUD plugin: portable paths, config, and per-chat colors.
Imported by the single hook entry point - no heavy dependencies.

Machine-local state (config + scratch: badge state, PID/alive files) lives in
``~/.claude/hal_voice``.
"""
import colorsys, hashlib, json, os

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
    "popup":       True,    # our own on-screen "a session needs you" card (Windows)
    "status_card": True,    # top-right per-chat card showing what each chat is working on (Windows)
    "notify":      True,    # native desktop toast (fallback off-Windows / when popup is off)
    "use_openai":  False,   # opt-in: name tabs with OpenAI. Off = Claude only (API key or CLI)
}

# Per-chat colors are assigned by SLOT - the position of a session among those currently
# open - not by hashing its id. Hashing collides (two of five tabs routinely share a color);
# slots don't. Each slot steps around the hue wheel by the GOLDEN ANGLE (~137.5 deg), which
# is the spacing that keeps *any* number of points maximally far apart: the first tabs land
# on wildly different hues, and colors only begin to resemble each other once ~a dozen tabs
# are open at once. slot_color(0) is green (the familiar "first session"), then violet,
# yellow, cyan, pink, lime, ... See hal_badge._assign_slot for how a session claims a slot.
_HUE_START = 145.0        # slot 0 -> green
_GOLDEN    = 137.50776    # golden angle, in degrees
_SAT       = 0.82         # vivid but not neon, reads well on a dark desktop
_VAL       = 1.0
FAIL_COLOR = (240, 80, 70)   # error red (reserved for a future 'failed' badge state)


def slot_color(slot):
    """Vivid, maximally-distinct accent for the session in slot ``slot`` (0, 1, 2, ...)."""
    try:
        slot = int(slot)
    except Exception:
        slot = 0
    hue = ((_HUE_START + slot * _GOLDEN) % 360.0) / 360.0
    r, g, b = colorsys.hsv_to_rgb(hue, _SAT, _VAL)
    return (round(r * 255), round(g * 255), round(b * 255))


def session_color(session_id):
    """Stable fallback accent (hash the id onto the same wheel) for callers that don't have a
    slot. The live badge path assigns real, collision-free slots instead - see
    ``hal_badge._assign_slot`` - so this is only a last resort."""
    if not session_id:
        return slot_color(0)
    h = int(hashlib.md5(str(session_id).encode("utf-8")).hexdigest(), 16)
    return slot_color(h % 64)


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
