#!/usr/bin/env python3
"""Stop hook: HAL 9000 always speaks. Picks the best-fitting pre-rendered pool line for
what was just done; if nothing fits, it proposes a new tailored line that is synthesized
and added to the pool in the background (so it plays instantly next time)."""
import json, os, random, subprocess
from datetime import datetime, timezone

ACTION_FILE     = os.path.join(os.path.expanduser("~"), ".claude", "last_claude_action.json")
STATUS_PID_FILE = os.path.join(os.path.expanduser("~"), ".claude", "status_popup_pid.txt")
HAL_POOL_DIR    = os.path.join(os.path.expanduser("~"), ".claude", "hal_pool")
MANIFEST        = os.path.join(HAL_POOL_DIR, "manifest.json")
PYTHON          = r"C:\Users\braxt\AppData\Local\Programs\Python\Python312\python.exe"
POPUP_SCRIPT    = os.path.join(os.path.dirname(__file__), "popup.ps1")
ADD_LINE_SCRIPT = os.path.join(os.path.dirname(__file__), "hal_add_line.py")
FFPLAY  = r"C:\Users\braxt\AppData\Local\Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-8.1.1-full_build\bin\ffplay.exe"
FFPROBE = r"C:\Users\braxt\AppData\Local\Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-8.1.1-full_build\bin\ffprobe.exe"

USER_NAME = "Braxton"

# Ultimate fallback only (pool not built yet / TTS unavailable): spoken via Windows SAPI.
HAL_FALLBACKS = [
    "I'm afraid that's quite done now, Braxton.",
    "The task has been completed.",
    "Everything is proceeding as I have foreseen.",
]


# ── context ───────────────────────────────────────────────────────────────────
def load_state():
    state = {}
    if os.path.exists(ACTION_FILE):
        try:
            raw = json.loads(open(ACTION_FILE).read())
            ts = datetime.fromisoformat(raw["ts"].replace("Z", "+00:00"))
            if (datetime.now(timezone.utc) - ts).total_seconds() < 600:
                state = raw
        except Exception:
            pass
        try:
            os.remove(ACTION_FILE)
        except Exception:
            pass
    return state


def build_context(state):
    parts = []
    if state.get("commit_msgs"):
        parts.append("git commit: " + "; ".join(state["commit_msgs"]))
    if state.get("scripts"):
        parts.append("ran: " + ", ".join(state["scripts"]))
    if state.get("files"):
        parts.append("files: " + ", ".join(state["files"][:6]))
    if state.get("actions"):
        parts.append("actions: " + ", ".join(state["actions"]))
    return " | ".join(parts) if parts else "general task completed"


# ── pool ──────────────────────────────────────────────────────────────────────
def load_pool():
    try:
        entries = json.loads(open(MANIFEST).read())
        return [e for e in entries if os.path.exists(os.path.join(HAL_POOL_DIR, e["file"]))]
    except Exception:
        return []


def llm_select(context, entries):
    """Ask haiku to pick the best-fitting existing line, and optionally propose a new one.
    Returns (pick_index, new_text_or_None). Falls back to (random, None) on any error."""
    try:
        import anthropic
        lines_block = "\n".join(f"{i+1}. {e['text']}" for i, e in enumerate(entries))
        prompt = (
            "You choose what HAL 9000 says aloud right after a task finished on Braxton's "
            "computer.\n"
            f"What was just done:\n  {context}\n\n"
            "Existing HAL 9000 lines you can choose from:\n"
            f"{lines_block}\n\n"
            "Pick the existing line that best fits what was done. STRONGLY prefer reusing an "
            "existing line - they are intentionally broad and almost always one fits.\n"
            "Reply in EXACTLY this format, two lines:\n"
            "PICK: <number of the best-fitting existing line>\n"
            "NEW: <NONE, or a single new HAL 9000 line - calm, eerie, 8 to 16 words, in HAL's "
            "measured voice, specific to what was done, naming Braxton about half the time - "
            "ONLY if genuinely none of the existing lines fit the situation>\n"
        )
        client = anthropic.Anthropic()
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=90,
            messages=[{"role": "user", "content": prompt}],
        )
        text = msg.content[0].text.strip()
        pick_idx, new_text = None, None
        for line in text.splitlines():
            s = line.strip()
            up = s.upper()
            if up.startswith("PICK:"):
                num = "".join(ch for ch in s[5:] if ch.isdigit())
                if num:
                    j = int(num) - 1
                    if 0 <= j < len(entries):
                        pick_idx = j
            elif up.startswith("NEW:"):
                val = s[4:].strip().strip('"').strip("'")
                if val and val.upper() != "NONE" and 3 < len(val) < 160:
                    new_text = val
        if pick_idx is None:
            pick_idx = random.randrange(len(entries))
        return pick_idx, new_text
    except Exception:
        return random.randrange(len(entries)), None


def spawn_add_line(text):
    """Background: synthesize a new HAL line and append it to the pool. Non-blocking."""
    try:
        subprocess.Popen(
            [PYTHON, ADD_LINE_SCRIPT, text],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=0x08000000,
        )
    except Exception:
        pass


# ── audio / ui ─────────────────────────────────────────────────────────────────
def get_duration_ms(path):
    try:
        r = subprocess.run(
            [FFPROBE, '-v', 'quiet', '-show_entries', 'format=duration',
             '-of', 'csv=p=0', path],
            capture_output=True, text=True, timeout=5
        )
        return int(float(r.stdout.strip()) * 1000) + 500
    except Exception:
        return 5000


def show_popup(text, duration_ms):
    subprocess.Popen(
        ["powershell", "-ExecutionPolicy", "Bypass", "-File", POPUP_SCRIPT,
         "-Text", text, "-DurationMs", str(duration_ms)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )


def play_mp3(path):
    abs_path = os.path.abspath(path)
    if os.path.exists(FFPLAY):
        subprocess.run([FFPLAY, "-nodisp", "-autoexit", abs_path],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
    else:
        safe = abs_path.replace("\\", "/")
        ps = (f"$wmp = New-Object -ComObject WMPlayer.OCX; $m = $wmp.newMedia('{safe}'); "
              "$wmp.currentPlaylist.appendItem($m); $wmp.controls.play(); Start-Sleep 6; $wmp.close()")
        subprocess.run(["powershell", "-Command", ps], capture_output=True, timeout=20)


def windows_tts_fallback(text):
    safe = text.replace("'", "")
    ps = ("Add-Type -AssemblyName System.Speech; "
          "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
          "$s.Rate = -2; "
          f"$s.Speak('{safe}'); $s.Dispose()")
    subprocess.run(["powershell", "-Command", ps], capture_output=True, timeout=15)


def kill_status_popup():
    try:
        if os.path.exists(STATUS_PID_FILE):
            pid = int(open(STATUS_PID_FILE).read().strip())
            subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                           capture_output=True, creationflags=0x08000000)
            try: os.remove(STATUS_PID_FILE)
            except Exception: pass
    except Exception:
        pass


def main():
    kill_status_popup()
    context = build_context(load_state())

    entries = load_pool()
    if not entries:
        # Pool not ready yet (still building) - say something rather than nothing.
        windows_tts_fallback(random.choice(HAL_FALLBACKS))
        return

    pick_idx, new_text = llm_select(context, entries)
    e = entries[pick_idx]
    phrase = e["text"]
    play_path = os.path.join(HAL_POOL_DIR, e["file"])
    duration_ms = get_duration_ms(play_path)

    show_popup(phrase, duration_ms)
    # If nothing fit well, synthesize the tailored line in the background for next time.
    if new_text:
        spawn_add_line(new_text)
    play_mp3(play_path)


if __name__ == "__main__":
    main()
