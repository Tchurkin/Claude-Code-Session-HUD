# claude-session-hud (Claude Code plugin)

An ambient HUD for running multiple Claude Code sessions at once: a per-chat colored badge
with live state (working / done / awaiting input) and an LLM-summarized name, a color
accent on the focused chat's VS Code window, and a new-chat-in-new-window button. Click a
badge to jump to that chat. See the [repo README](../../README.md) for the full picture.

## Layout
- `hooks/hooks.json` — every hook event routes to one dispatcher, `scripts/hal_badge.py`.
- `scripts/`
  - `hal_badge.py` — the dispatcher + badge controller (state, LLM naming, spawns the helpers).
  - `badge.ps1` — the per-chat badge windows (color, state indicator, click-to-focus, stacking).
  - `hal_tint.ps1` — the focused-window color accent overlay.
  - `claude_button.ps1` — the always-on-top "new chat in a new window" button.
  - `popup_common.ps1` — shared layered-window / Win32 helpers.
  - `hal_common.py` — paths, config, per-chat colors.

## Config
`~/.claude/hal_voice/config.json`: `badge`, `window_tint`, `button` (all default true).

Windows-only today (WPF/Win32 UI).
