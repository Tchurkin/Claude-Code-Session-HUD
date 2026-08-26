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

# How long an overlay or the daemon may go without a heartbeat before a watcher calls it dead and
# either reaps or replaces it. Lives here because everything that watches anything needs it, and it
# was previously copied out four times - which is precisely how the daemon came to beat every 12
# seconds against a 9 second threshold and look like a corpse to every watcher, forever, unnoticed.
# The PowerShell side keeps its own copy in popup_common.ps1; a test asserts the two agree.
DAEMON_STALE_MS = 9000

_DEFAULTS = {
    "enabled":     True,    # master on/off for the whole HUD (flip via the VS Code status-bar extension)
    "badge":       True,    # persistent per-chat color badge window (bottom-right)
    "window_tint": True,    # colored accent bar on the focused chat's VS Code window
    "popup":       True,    # our own on-screen "a session needs you" card (Windows)
    "status_card": True,    # top-right per-chat card showing what each chat is working on (Windows)
    "notify":      True,    # native desktop toast (fallback off-Windows / when popup is off)
    "sound":       True,    # short chime when a chat needs you (asking / blocked / finished)
    "usage_meter": True,    # show how much of the 5-hour session window is used (needs a login)
    "usage_alert": True,    # chime + card the moment the burn will run the session out early
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

# The accent is the tab's TEXT, drawn on a near-black chip, so it has to be light enough to read.
# Hue alone doesn't decide that: at full saturation a blue is barely brighter than the chip it sits
# on (contrast ~2:1) while a yellow of the same saturation is ~12:1. So every slot is desaturated
# toward white until it clears a legibility floor - dark hues become pastels, hues that were already
# bright are untouched, and all of them keep their identity.
_CHIP_BG      = (44, 44, 44)   # the chip's background when lit (its lightest, so the worst case)
_MIN_CONTRAST = 4.5            # WCAG AA for normal text


def _linear(c):
    c /= 255.0
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def relative_luminance(rgb):
    r, g, b = (_linear(float(x)) for x in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(fg, bg):
    a, b = relative_luminance(fg), relative_luminance(bg)
    hi, lo = max(a, b), min(a, b)
    return (hi + 0.05) / (lo + 0.05)


def slot_color(slot):
    """Distinct, legible accent for the session in slot ``slot`` (0, 1, 2, ...)."""
    try:
        slot = int(slot)
    except Exception:
        slot = 0
    hue = ((_HUE_START + slot * _GOLDEN) % 360.0) / 360.0
    sat = _SAT
    while True:
        r, g, b = colorsys.hsv_to_rgb(hue, sat, _VAL)
        rgb = (round(r * 255), round(g * 255), round(b * 255))
        if sat <= 0.06 or contrast_ratio(rgb, _CHIP_BG) >= _MIN_CONTRAST:
            return rgb
        sat -= 0.02


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
    # utf-8-sig, not utf-8: this file is shared with PowerShell and the VS Code extension, and a
    # byte-order mark left by an editor (or by `Set-Content -Encoding utf8`) would otherwise make the
    # whole config unreadable - which reads as "every setting is at its default" and is a maddening
    # thing to debug. -sig tolerates a BOM and is identical without one.
    cfg = dict(_DEFAULTS)
    try:
        with open(CONFIG_PATH, encoding="utf-8-sig") as f:
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
