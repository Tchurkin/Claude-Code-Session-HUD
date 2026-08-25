# Claude Session HUD — VS Code companion

A small VS Code extension that does the two things the
[Claude Session HUD](https://github.com/Tchurkin/Claude-Code-Session-HUD) plugin can't do from
outside the editor.

**1 — A bottom-bar on/off switch.** A Claude Code plugin can't add a status-bar item itself, so
this provides one. It shares no code with the plugin: it just flips the same `enabled` flag in
`~/.claude/hal_voice/config.json` that the HUD's overlays already watch. Click **`⧉ HUD`** to turn
the whole HUD off; click again for on. The badges, window tint, cards, and floating button react on
their own, and it stays in sync both ways — flipping the corner button or editing the config updates
the status-bar item too.

**2 — Window/tab liaison.** From outside, all the HUD can see of a window is its title, and a title
names only the tab that's in front. This extension reports which chats each window is actually
holding (so a badge can point at the right window even for a chat in a background tab), and when you
click a badge it brings that chat's tab to the front and puts the cursor in its input — the badge
raises the window, this switches to the chat inside it. Without the extension you still get the
window, just not the tab.

It reads `focus.json` and writes `windows/w<pid>.json` under `~/.claude/hal_voice/` — no network, no
other side effects.

## Install

From this folder:

```powershell
npm run package                       # produces a .vsix
code --install-extension claude-session-hud-statusbar-0.2.0.vsix
```

Or for a quick try without packaging: open this folder in VS Code and press **F5** (Extension
Development Host).

After installing, reload VS Code. You'll see **`⧉ HUD`** at the right of the status bar. Each window
picks the extension up when it next reloads, so jump-to-tab starts working per window as you go.

## Tip

Once you're using the status-bar toggle, you can hide the floating corner button by setting
`"toggle": false` in `~/.claude/hal_voice/config.json`.
