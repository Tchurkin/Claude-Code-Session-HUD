# Claude Session HUD

**An ambient heads-up display for running many Claude Code sessions at once.**

When you've got several Claude Code chats going in parallel, you lose track of which one
is which, which is still working, and — the big one — **which one is blocked waiting for
you**. This is a lightweight, hook-based HUD that answers that at a glance, layered over
the sessions you already run. No new app, no orchestrator, no worktree management: it just
watches Claude Code's hooks and draws a small always-on-top UI.

> A real Claude Code **plugin** (`plugins/hal-voice`), distributed from this repo's
> marketplace. **Windows-only today** (the UI is WPF/Win32).

## What you get

| Feature | What it does |
|---|---|
| **Per-chat badge** | A small persistent chip, bottom-right, one per chat — in that chat's own color. |
| **Live state** | The badge shows **✓ done**, a **breathing dot = working**, or a **blinking ring = awaiting your input** (permission / idle). This is the "which session needs me?" signal. |
| **Smart name** | The badge is labelled with a 1–3 word summary of what the chat has been working on (from the transcript, via an LLM), not just the folder name. |
| **Click to jump** | Left-click a badge to focus that chat's VS Code window; right-click to dismiss it. |
| **Window color-coding** | The focused chat's VS Code window gets a matching color accent along its top edge. |
| **New-window button** | An always-on-top spark button; click it to open a **new chat in a new window** (so each chat is its own window and the click-to-jump lands precisely). |

Badges stack, so several chats form a tidy dock; the button rides on top of the stack.

## How it works

Everything is driven by Claude Code **hooks** → one dispatcher (`scripts/hal_badge.py`):

- `SessionStart` / `UserPromptSubmit` → mark the chat, capture its window, refresh its name
- `PreToolUse` / `PostToolUse` → keep the badge and helpers alive while it works
- `Notification` → mark the chat **awaiting input**
- `Stop` → mark the chat **done**

The dispatcher writes tiny per-chat state files under `~/.claude/hal_voice/`. Three small
always-on-top helpers render from that state and clean themselves up:
`badge.ps1` (the badges), `hal_tint.ps1` (the window accent), `claude_button.ps1` (the
button). Shared Win32/layered-window helpers live in `scripts/popup_common.ps1`.

The name summary uses an LLM: it reads `OPENAI_API_KEY` (or `~/.claude/.openai_key`), or
falls back to `ANTHROPIC_API_KEY`, and degrades to a keyword theme if neither is present.

## Install

```powershell
git clone https://github.com/Tchurkin/hal-voice-bundle
cd hal-voice-bundle
/plugin marketplace add C:\path\to\hal-voice-bundle
/plugin install claude-session-hud@session-hud
```
(For dev iteration: `claude --plugin-dir C:\path\to\hal-voice-bundle\plugins\hal-voice`.)

Needs a `python` on PATH for the hooks (no third-party packages). Reload Claude Code so the
hooks load. Optional: set `OPENAI_API_KEY` for the sharpest chat names.

## Config (`~/.claude/hal_voice/config.json`)

| key | meaning |
|---|---|
| `badge` | show the per-chat badges (default true) |
| `window_tint` | color-accent the focused chat window (default true) |
| `button` | show the new-window button (default true) |

## Limitations & roadmap

- **Windows-only** right now. macOS/Linux is the biggest thing that would broaden it.
- **Click-to-jump / window accent are VS Code + Windows specific** (they use window
  handles). The core state HUD (which chat is working / done / waiting) is universal and
  is the part worth generalizing first.
- The **"awaiting input"** signal is the highest-value piece — it maps to open Claude Code
  feature requests for knowing which parallel session is blocked.

PRs / issues welcome — especially cross-platform rendering and better "waiting for input"
detection.

## Notes

- The plugin folder is still named `hal-voice` (this started life as a HAL-9000 voice
  notifier); the voice half has been removed. Renaming the folder/repo is a cosmetic
  follow-up (update the hook paths in `~/.claude/settings.json` if you do).
- MIT licensed.
