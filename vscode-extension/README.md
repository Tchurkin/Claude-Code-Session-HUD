# Claude Session HUD — Status Bar Toggle

A tiny VS Code extension that adds a **bottom-bar on/off switch** for the
[Claude Session HUD](https://github.com/Tchurkin/Claude-Code-Session-HUD) plugin.

A Claude Code plugin can't add a status-bar item itself (the status bar belongs to VS Code
extensions), so this companion extension provides one. It shares no code with the plugin — it
just flips the same `enabled` flag in `~/.claude/hal_voice/config.json` that the HUD's overlays
already watch. Click the **`⧉ HUD`** item to turn the whole HUD off; click again to turn it on.
The badges, window tint, cards, and floating button react on their own.

It stays in sync both ways: flipping the corner toggle button (or editing the config) updates
the status-bar item too.

## Install

From this folder:

```powershell
npm run package                       # produces a .vsix
code --install-extension claude-session-hud-statusbar-0.1.0.vsix
```

Or for a quick try without packaging: open this folder in VS Code and press **F5** (Extension
Development Host).

After installing, reload VS Code. You'll see **`⧉ HUD`** at the right of the status bar.

## Tip

Once you're using the status-bar toggle, you can hide the floating corner button by setting
`"toggle": false` in `~/.claude/hal_voice/config.json`.
