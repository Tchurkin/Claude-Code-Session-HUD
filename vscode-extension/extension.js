// Claude Session HUD — VS Code companion.
//
// Two jobs, both things a Claude Code plugin can't do from outside the editor:
//
//  1. A status-bar on/off switch. It shares no code with the plugin: it just flips the same
//     `enabled` flag in the plugin's config file that the HUD overlays already poll (see
//     popup_common.ps1 -> Hud-Enabled). Click it to turn the whole HUD off/on.
//
//  2. Window/tab liaison. From outside, all the HUD can see of a window is its title, which names
//     only the tab that's currently in front. This reports what chats each window actually holds
//     (so a badge can point at the right window even for a chat sitting in a background tab), and
//     when you click a badge, brings that chat's tab to the front and puts the cursor in it — the
//     badge raises the window, this switches to the chat inside it.

const vscode = require('vscode');
const fs = require('fs');
const os = require('os');
const path = require('path');

const CONFIG_DIR = path.join(os.homedir(), '.claude', 'hal_voice');
const CONFIG = path.join(CONFIG_DIR, 'config.json');
const WINDOWS_DIR = path.join(CONFIG_DIR, 'windows');
const FOCUS = path.join(CONFIG_DIR, 'focus.json');
const MY_REPORT = path.join(WINDOWS_DIR, `w${process.pid}.json`);   // one extension host = one window

const HEARTBEAT_MS = 15000;   // so the HUD can tell a closed window from a quiet one
const POLL_MS = 500;          // backstop for the focus request, in case fs.watch misses it

// These files are shared with Python and PowerShell. A byte-order mark from an editor (or from
// `Set-Content -Encoding utf8`, which adds one) makes JSON.parse throw on the very first character,
// so strip it rather than silently falling back to defaults.
function readJson(file) {
  try {
    return JSON.parse(fs.readFileSync(file, 'utf8').replace(/^\uFEFF/, ''));
  } catch (e) {
    return null;
  }
}

function readEnabled() {
  const c = readJson(CONFIG);
  return !c || c.enabled !== false;      // default ON when unreadable or the key is absent
}

function writeEnabled(val) {
  const c = readJson(CONFIG) || {};
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

// ── what this window is holding ────────────────────────────────────────────────
// Tab labels are truncated by VS Code ("Analyze college admissio…"), and so is the chat title the
// HUD asks for, at a different length. So compare on the ellipsis-free stem, and let either side be
// the shorter one — a prefix match in whichever direction is the most either can honestly claim.
const norm = (s) => String(s || '').replace(/\s+/g, ' ').trim().toLowerCase()
  .replace(/[….\s]+$/, '');

function sameChat(a, b) {
  const x = norm(a), y = norm(b);
  if (!x || !y) return false;
  if (x === y) return true;
  const short = x.length < y.length ? x : y;
  return short.length >= 8 && (x.startsWith(y) || y.startsWith(x));
}

function allTabs() {
  const out = [];
  for (const g of vscode.window.tabGroups.all) for (const t of g.tabs) out.push({ group: g, tab: t });
  return out;
}

function report() {
  // The HUD pairs this with the OS window list: the window whose title leads with our active tab is
  // us, and then every chat in `tabs` is known to live there - background ones included.
  let active = '';
  try {
    const g = vscode.window.tabGroups.activeTabGroup;
    active = (g && g.activeTab && g.activeTab.label) || '';
  } catch (e) { /* older API */ }
  const body = {
    ts: Date.now(),
    pid: process.pid,
    folder: (vscode.workspace.workspaceFolders || []).map((f) => f.name).join(', '),
    active,
    tabs: allTabs().map((x) => x.tab.label),
  };
  try {
    fs.mkdirSync(WINDOWS_DIR, { recursive: true });
    const tmp = MY_REPORT + '.tmp';
    fs.writeFileSync(tmp, JSON.stringify(body));
    fs.renameSync(tmp, MY_REPORT);        // atomic: the HUD reads this constantly
  } catch (e) { /* best effort */ }
}

// ── bring a chat's tab to the front ────────────────────────────────────────────
function isActive(label) {
  try {
    const g = vscode.window.tabGroups.activeTabGroup;
    return !!(g && g.activeTab && g.activeTab.label === label);
  } catch (e) {
    return false;
  }
}

const GROUP_CMDS = ['First', 'Second', 'Third', 'Fourth', 'Fifth', 'Sixth', 'Seventh', 'Eighth']
  .map((n) => `workbench.action.focus${n}EditorGroup`);

async function reveal(group, tab) {
  const label = tab.label;
  const col = group.viewColumn;
  if (col >= 1 && col <= GROUP_CMDS.length) {
    try { await vscode.commands.executeCommand(GROUP_CMDS[col - 1]); } catch (e) { /* single group */ }
  }
  if (isActive(label)) return true;
  // openEditorAtIndex has been both 0- and 1-based across versions; try each and check the result
  // rather than betting on one, then fall back to stepping through the group.
  const idx = group.tabs.indexOf(tab);
  if (idx >= 0) {
    for (const arg of [idx, idx + 1]) {
      try { await vscode.commands.executeCommand('workbench.action.openEditorAtIndex', arg); } catch (e) { /* try next */ }
      if (isActive(label)) return true;
    }
  }
  for (let i = 0; i < group.tabs.length; i++) {
    try { await vscode.commands.executeCommand('workbench.action.nextEditorInGroup'); } catch (e) { break; }
    if (isActive(label)) return true;
  }
  return isActive(label);
}

let lastFocusTs = 0;

async function handleFocusRequest() {
  const req = readJson(FOCUS);
  if (!req || !req.title || !req.ts || req.ts === lastFocusTs) return;
  lastFocusTs = req.ts;
  if (Date.now() - req.ts > 15000) return;      // stale (e.g. left over from before we started)
  if (norm(req.title).length < 3) return;
  const hit = allTabs().find((x) => sameChat(x.tab.label, req.title));
  if (!hit) return;                              // that chat isn't in this window - another one has it
  await reveal(hit.group, hit.tab);
  try {                                          // land in the chat's input box, not just its tab
    await vscode.commands.executeCommand('claude-vscode.focus');
  } catch (e) { /* not a Claude tab, or an older extension */ }
  report();
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

  // Reflect changes made elsewhere (the corner button, or hand-editing config.json), and pick up
  // focus requests the moment a badge writes one.
  try {
    const watcher = fs.watch(CONFIG_DIR, (ev, f) => {
      if (f === 'config.json') render();
      else if (f === 'focus.json') handleFocusRequest();
    });
    context.subscriptions.push({ dispose: () => { try { watcher.close(); } catch (e) {} } });
  } catch (e) { /* fs.watch may be unavailable; the polls below cover it */ }

  const poll = setInterval(render, 2000);
  const focusPoll = setInterval(handleFocusRequest, POLL_MS);
  const beat = setInterval(report, HEARTBEAT_MS);
  context.subscriptions.push({
    dispose: () => { clearInterval(poll); clearInterval(focusPoll); clearInterval(beat); },
  });

  // Re-report whenever the set of tabs, or which one is in front, changes.
  const bump = () => { try { report(); } catch (e) {} };
  try {
    context.subscriptions.push(vscode.window.tabGroups.onDidChangeTabs(bump));
    context.subscriptions.push(vscode.window.tabGroups.onDidChangeTabGroups(bump));
  } catch (e) { /* pre-1.68 has no tabGroups API: the HUD falls back to window titles */ }
  context.subscriptions.push(vscode.window.onDidChangeWindowState(bump));
  bump();

  context.subscriptions.push({
    dispose: () => { try { fs.unlinkSync(MY_REPORT); } catch (e) {} },
  });
}

function deactivate() {
  try { fs.unlinkSync(MY_REPORT); } catch (e) {}
}

module.exports = { activate, deactivate };
