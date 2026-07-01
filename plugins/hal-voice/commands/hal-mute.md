---
description: Silence HAL (voice + popups) on this machine until /hal-unmute
---

Mute the HAL voice plugin on THIS machine — no spoken announcements and no popups — until the user runs `/hal-unmute`. The hooks read the flag live, so no reload is needed.

1. Read `~/.claude/hal_voice/config.json` to get `pool_repo` (and `tts_python`).
2. Preferred: run the helper (plain `python` is fine; use `<tts_python>` only if `python` isn't on PATH):

   `python "<pool_repo>/plugins/hal-voice/scripts/hal_mute.py" on`

3. Fallback (if `pool_repo` is null or the script can't be found): set `"muted": true` directly in `~/.claude/hal_voice/config.json`, creating the file as `{"muted": true}` if it doesn't exist.
4. Confirm to the user that HAL is now muted.
