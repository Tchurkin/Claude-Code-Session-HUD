#!/usr/bin/env python3
"""
Stop hook: HAL 9000 speaks when a response finishes.

Flow:
  1. Build context from what Claude just did (recorded by track_action.py).
  2. Ask an LLM to pick the best-fitting line from the pool and, if nothing really
     fits, propose a single new tailored line.
  3. If this machine can synthesize live (configured F5 venv + GPU) and a tailored line
     was proposed, ask the warm daemon for it and WAIT up to `synth_budget_ms`:
        - ready in time -> play the fresh, tailored line.
        - too slow      -> play the best pool line now; the daemon finishes the new
                           line into the pool so it's instant next time.
  4. Otherwise just play the best pool line.

Runs under the user's plain `python` (no ML stack) - the heavy work is delegated to the
daemon via a light socket client.  Degrades gracefully at every step.
"""
import json, os, random, sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hal_common as hc

# Spoken via the built-in Windows voice only if the pool is completely empty.
HARD_FALLBACKS = [
    "I'm afraid that's quite done now.",
    "The task has been completed.",
    "Everything is proceeding as I have foreseen.",
]


# ── context ────────────────────────────────────────────────────────────────────
def read_payload():
    """The Stop hook pipes its JSON on stdin; grab the whole payload (session id, cwd, ...)."""
    try:
        raw = sys.stdin.read()
        if raw:
            return json.loads(raw.lstrip("﻿"))
    except Exception:
        pass
    return {}


def load_state():
    state = {}
    try:
        raw = json.loads(open(hc.ACTION_FILE, encoding="utf-8").read())
        ts = datetime.fromisoformat(raw["ts"].replace("Z", "+00:00"))
        if (datetime.now(timezone.utc) - ts).total_seconds() < 600:
            state = raw
    except Exception:
        pass
    try: os.remove(hc.ACTION_FILE)
    except Exception: pass
    return state


def build_context(state):
    parts = []
    if state.get("commit_msgs"): parts.append("git commit: " + "; ".join(state["commit_msgs"]))
    if state.get("scripts"):     parts.append("ran: " + ", ".join(state["scripts"]))
    if state.get("files"):       parts.append("files: " + ", ".join(state["files"][:6]))
    if state.get("actions"):     parts.append("actions: " + ", ".join(state["actions"]))
    return " | ".join(parts) if parts else "general task completed"


# ── LLM line selection (optional; degrades to random) ──────────────────────────
def llm_select(context, entries, user_name, mood="done"):
    try:
        import anthropic
    except Exception:
        return random.randrange(len(entries)), None
    try:
        lines_block = "\n".join(f"{i+1}. {e['text']}" for i, e in enumerate(entries))
        name_clause = (f"naming {user_name} about half the time, " if user_name else "")
        mood_clause = (
            "The task FAILED or hit an error. HAL stays calm and unhurried but sounds "
            "quietly concerned or faintly ominous about the problem - never cheerful."
            if mood == "fail" else
            "The task finished successfully; HAL is composed and quietly self-assured."
        )
        prompt = (
            "You choose what HAL 9000 says aloud right after a task finished on the "
            "user's computer.\n"
            f"What was just done:\n  {context}\n"
            f"Tone: {mood_clause}\n\n"
            "Existing HAL 9000 lines you can choose from:\n"
            f"{lines_block}\n\n"
            "Pick the existing line that best fits. STRONGLY prefer reusing an existing "
            "line - they are intentionally broad and almost always one fits.\n"
            "Reply in EXACTLY this format, two lines:\n"
            "PICK: <number of the best-fitting existing line>\n"
            "NEW: <NONE, or a single new HAL 9000 line - calm, eerie, 8 to 16 words, in "
            f"HAL's measured voice, specific to what was done and matching the tone above, "
            f"{name_clause}ONLY if genuinely none of the existing lines fit>\n"
        )
        # Short timeout: if the API hangs, fail fast to random pool selection rather
        # than letting the async Stop hook get killed at 120s with HAL never speaking.
        msg = anthropic.Anthropic(timeout=6.0).messages.create(
            model="claude-haiku-4-5-20251001", max_tokens=90,
            messages=[{"role": "user", "content": prompt}])
        text = msg.content[0].text.strip()
        pick_idx, new_text = None, None
        for line in text.splitlines():
            s = line.strip(); up = s.upper()
            if up.startswith("PICK:"):
                num = "".join(ch for ch in s[5:] if ch.isdigit())
                if num and 0 <= int(num) - 1 < len(entries):
                    pick_idx = int(num) - 1
            elif up.startswith("NEW:"):
                val = s[4:].strip().strip('"').strip("'")
                if val and val.upper() != "NONE" and 3 < len(val) < 160:
                    new_text = val
        if pick_idx is None:
            pick_idx = random.randrange(len(entries))
        return pick_idx, new_text
    except Exception:
        return random.randrange(len(entries)), None


def main():
    cfg = hc.load_config()
    payload = read_payload()
    session_id = payload.get("session_id")
    try:
        import hal_badge; hal_badge.touch(session_id, payload.get("cwd"), state="done")
    except Exception:
        pass
    hc.kill_status_popup(session_id)   # clear only THIS chat's in-progress popup
    if hc.is_muted(cfg):           # /hal-mute: stay silent (the popup kill above is enough)
        return

    state = load_state()
    context = build_context(state)
    pdir = hc.pool_dir(cfg)
    entries = hc.load_pool(pdir)
    if not entries:
        hc.windows_tts(random.choice(HARD_FALLBACKS))
        return

    # Tone follows the last informative tool result this turn (set by track_action).
    mood = "fail" if state.get("last_failed") else "done"
    candidates = hc.pool_by_kind(entries, mood)
    if not candidates:             # e.g. no 'fail' lines rendered yet -> use completion lines
        candidates = hc.pool_by_kind(entries, "done") or entries

    pick_idx, new_text = llm_select(context, candidates, cfg.get("user_name"), mood=mood)
    chosen = candidates[pick_idx]
    phrase = chosen["text"]
    play_path = os.path.join(pdir, chosen["file"])
    dur_ms = hc.entry_duration_ms(chosen)

    # Try a live, tailored line if proposed and this machine can synthesize fast.
    if new_text and hc.can_synth_live(cfg):
        try:
            import hal_tts_f5
            fresh_path, fresh_dur = hal_tts_f5.request_synth(
                new_text, int(cfg.get("synth_budget_ms", 6000)), cfg,
                kind=(mood if mood != "done" else None))
            if fresh_path:
                phrase, play_path, dur_ms = new_text, fresh_path, (fresh_dur or dur_ms)
        except Exception:
            pass  # any failure -> fall through to the chosen pool line

    accent = hc.FAIL_COLOR if mood == "fail" else hc.session_color(session_id)
    hc.show_completion_popup(phrase, dur_ms + 500, accent=accent)
    hc.play_audio(play_path, wait=True, timeout=max(15, dur_ms // 1000 + 8))


if __name__ == "__main__":
    main()
