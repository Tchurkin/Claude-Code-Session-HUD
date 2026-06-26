---
description: Sync the HAL voice pool with your other devices over git
---

Sync the shared HAL voice pool (push lines this machine made, pull lines other devices made).

1. Read `~/.claude/hal_voice/config.json` to get `pool_repo` (and `tts_python`, though plain `python` is fine for sync).
2. If `pool_repo` is null, tell the user to run `/hal-setup` from their git clone first.
3. Run:

   `python "<pool_repo>/plugins/hal-voice/scripts/hal_pool_sync.py"`

   (use `<tts_python>` if plain `python` isn't on PATH)
4. Report what it printed (committed / pulled / pushed, and the final pool line count). If it reports a push failure due to no write access, that's expected for users who only consume the pool — they still pulled the latest lines.
