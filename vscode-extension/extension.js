// Claude Session HUD — VS Code status-bar toggle.
//
// A Claude Code plugin can't add a status-bar item, so this tiny companion extension does.
// It shares no code with the plugin: it just flips the same `enabled` flag in the plugin's
// config file that the HUD overlays already poll (see popup_common.ps1 -> Hud-Enabled). Click
// the item to turn the whole HUD off/on; the badges, window tint, cards, and floating button
// react on their own.

const vscode = require('vscode');
const fs = require('fs');
const os = require('os');
const path = require('path');

const CONFIG_DIR = path.join(os.homedir(), '.claude', 'hal_voice');
const CONFIG = path.join(CONFIG_DIR, 'config.json');

function readEnabled() {
  try {
    const c = JSON.parse(fs.readFileSync(CONFIG, 'utf8'));
    return c.enabled !== false;          // default ON when the key is absent
  } catch (e) {
    return true;
  }
}

function writeEnabled(val) {
  let c = {};
  try { c = JSON.parse(fs.readFileSync(CONFIG, 'utf8')) || {}; } catch (e) { /* start fresh */ }
  c.enabled = !!val;
  try {
    fs.mkdirSync(CONFIG_DIR, { recursive: true });
    fs.writeFileSync(CONFIG, JSON.stringify(c, null, 2));
  } catch (e) {
    vscode.window.showErrorMessage('Claude HUD: could not write ' + CONFIG + ' — ' + e.message);
  }
}

let item;

function render() {
  const on = readEnabled();
  item.text = on ? '$(broadcast) HUD' : '$(circle-slash) HUD';
  item.tooltip = on
    ? 'Claude Session HUD is ON — click to turn off'
    : 'Claude Session HUD is OFF — click to turn on';
  item.color = on ? undefined : new vscode.ThemeColor('disabledForeground');
}

function activate(context) {
  item = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Right, 100);
  item.command = 'claudeHud.toggle';
  context.subscriptions.push(item);

  context.subscriptions.push(
    vscode.commands.registerCommand('claudeHud.toggle', () => {
      writeEnabled(!readEnabled());
      render();
    })
  );

  render();
  item.show();

  // Reflect changes made elsewhere (the corner button, or hand-editing config.json).
  try {
    const watcher = fs.watch(CONFIG_DIR, (ev, f) => { if (f === 'config.json') render(); });
    context.subscriptions.push({ dispose: () => { try { watcher.close(); } catch (e) {} } });
  } catch (e) { /* fs.watch may be unavailable; the poll below covers it */ }

  const poll = setInterval(render, 2000);
  context.subscriptions.push({ dispose: () => clearInterval(poll) });
}

function deactivate() {}

module.exports = { activate, deactivate };
