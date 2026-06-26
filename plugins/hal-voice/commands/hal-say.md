---
description: Make HAL speak a line (synthesizes it live + adds it to the pool if new)
argument-hint: <the line for HAL to say>
---

Have HAL 9000 speak this line, synthesizing it with F5-TTS and adding it to the shared pool if it's new: **$ARGUMENTS**

1. Read `~/.claude/hal_voice/config.json`. Use the `tts_python` field as the interpreter and `pool_repo` to locate the scripts.
2. If `tts_python` is null/missing, tell the user live synthesis isn't configured on this machine (run `/hal-setup` on a GPU machine with the f5-tts venv) — HAL can only play existing pool lines here.
3. Otherwise run:

   `<tts_python> "<pool_repo>/plugins/hal-voice/scripts/hal_add_line.py" "$ARGUMENTS" --play`

   The first run after a reboot loads the model (~10-20s); after that the warm daemon answers in a few seconds.
4. Report the saved file path. Suggest `/hal-sync` if they want the new line on their other devices.
