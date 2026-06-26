# hal-voice (Claude Code plugin)

HAL 9000 speaks when Claude finishes a response, with live F5-TTS synthesis on a GPU and
a pre-rendered, git-shared voice pool as fallback. See the [repo README](../../README.md)
for full install, architecture, and config.

## Layout
- `hooks/hooks.json` — `Stop` (announcer), `PreToolUse`/`PostToolUse` (status popups).
- `scripts/` — `hal_announce.py` (Stop), `hal_tts_f5.py` (F5 synth + warm daemon),
  `hal_common.py` (shared helpers), `hal_setup.py`, `hal_add_line.py`, `hal_pool_sync.py`,
  `render_pool_gpu.py`, the PowerShell popups + `play_audio.ps1`.
- `reference/` — voice clone reference (wav + transcript) and the RNNoise model.
- `hal_pool/` — the shared pool (mp3s + `manifest.json`); the writable copy lives in your
  git clone and is synced across devices.

## Commands
`/hal-setup`, `/hal-say <line>`, `/hal-sync`.

Runtime config: `~/.claude/hal_voice/config.json` (written by `hal_setup.py`).
