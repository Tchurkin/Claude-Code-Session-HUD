---
description: Re-enable HAL's voice and popups after /hal-mute
---

Unmute the HAL voice plugin on THIS machine so it speaks again. The hooks read the flag live, so no reload is needed.

1. Read `~/.claude/hal_voice/config.json` to get `pool_repo` (and `tts_python`).
2. Preferred: run the helper (plain `python` is fine; use `<tts_python>` only if `python` isn't on PATH):

   `python "<pool_repo>/plugins/hal-voice/scripts/hal_mute.py" off`

3. Fallback (if `pool_repo` is null or the script can't be found): set `"muted": false` directly in `~/.claude/hal_voice/config.json`.
4. Confirm to the user that HAL will speak again.
