#!/usr/bin/env python3
"""
Notification hook: HAL speaks when Claude is waiting on you.

Claude Code fires the Notification hook when it needs the user - a permission prompt, or
when it has been idle waiting for input.  HAL acknowledges with an "awaiting input" line.

Like the Stop announcer this runs under the user's plain `python` (no ML stack) and
degrades gracefully:
  * a pre-rendered 'wait' pool line plays instantly;
  * else, on a machine that can synthesize live, the line is made on the fly (and lands
    in the pool for next time);
  * else a silent on-screen popup still tells you HAL is waiting.

A short debounce stops a burst of permission prompts from talking over each other.
"""
import json, os, random, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hal_common as hc
import hal_lines

DEBOUNCE_S = 25
LAST_FILE  = os.path.join(hc.DATA_DIR, "last_notify.txt")


def _debounced():
    """True if we spoke a notification very recently (and we should stay quiet now)."""
    try:
        if time.time() - float(open(LAST_FILE).read().strip()) < DEBOUNCE_S:
            return True
    except Exception:
        pass
    try:
        hc.ensure_data_dir()
        with open(LAST_FILE, "w") as f:
            f.write(str(time.time()))
    except Exception:
        pass
    return False


def _payload():
    try:
        raw = sys.stdin.read()
        if raw:
            return json.loads(raw.lstrip("﻿"))
    except Exception:
        pass
    return {}


def main():
    cfg = hc.load_config()
    payload = _payload()
    session_id = payload.get("session_id")
    try:
        import hal_badge; hal_badge.touch(session_id, payload.get("cwd"), state="waiting")
    except Exception:
        pass
    if hc.is_muted(cfg) or _debounced():
        return

    pdir = hc.pool_dir(cfg)
    waits = hc.pool_by_kind(hc.load_pool(pdir), "wait")
    play_path, dur_ms = None, 5000

    if waits:
        e = random.choice(waits)
        phrase  = e["text"]
        play_path, dur_ms = os.path.join(pdir, e["file"]), hc.entry_duration_ms(e)
    else:
        # No 'wait' lines baked yet: synthesize one live if we can, else show a silent popup.
        phrase = random.choice(hal_lines.for_name(hal_lines.WAIT_LINES, cfg.get("user_name")))
        if hc.can_synth_live(cfg):
            try:
                import hal_tts_f5
                p, d = hal_tts_f5.request_synth(
                    phrase, int(cfg.get("synth_budget_ms", 6000)), cfg, kind="wait")
                if p:
                    play_path, dur_ms = p, (d or dur_ms)
            except Exception:
                pass

    hc.show_completion_popup(phrase, dur_ms + 500, accent=hc.session_color(session_id))
    if play_path:
        hc.play_audio(play_path, wait=True, timeout=max(12, dur_ms // 1000 + 8))


if __name__ == "__main__":
    main()
