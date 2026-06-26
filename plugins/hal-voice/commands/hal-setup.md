---
description: Configure the HAL voice plugin on this machine (detects GPU + F5 venv)
---

Set up the HAL voice plugin for THIS machine.

1. Find the user's git clone of the `hal-voice-bundle` repo (it contains `plugins/hal-voice/scripts/hal_setup.py`). If you can't find it, ask the user for its path.
2. Determine the interpreter:
   - If `<repo>/venv/Scripts/python.exe` exists, use it (so capability detection can import torch).
   - Otherwise use `python`.
3. Run, passing the user's preferred name if they gave one in `$ARGUMENTS` (default: keep existing / "Braxton"):

   `<interpreter> "<repo>/plugins/hal-voice/scripts/hal_setup.py" --name <NAME>`

4. Show the user the printed config summary. If `live synth` is OFF and they expected it ON, check that the `venv` exists and has `f5-tts` + CUDA torch (see the repo README "Regenerate the voice pool on a GPU").
5. Remind them to reload: `/reload-plugins` (or restart Claude Code) so the hooks pick up the new config.
