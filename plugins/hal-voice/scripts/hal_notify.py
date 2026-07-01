#!/usr/bin/env python3
"""
Cross-platform, dependency-free desktop notifications for the session HUD.

The Windows badges answer "which session needs me?" only while you're looking at the
screen. The highest-value moment - a background session going **blocked, waiting for you**
- deserves a push you'll catch even when its window is minimized or you're in another app,
and it's the one part of the HUD that can work on *every* OS with no extra install.

``notify(...)`` maps a session state transition to a native toast:
  * macOS   -> ``osascript`` (display notification)
  * Linux   -> ``notify-send`` (fallback: ``zenity --notification``)
  * Windows -> WinRT toast via PowerShell (Win10/11, no modules)

Every backend is best-effort and fully guarded: a missing tool or any error is swallowed so
a notification never breaks a hook. De-duping (only fire on the *transition* into a state,
not every hook) is the caller's job - see ``hal_badge.main``.
"""
import os, platform, subprocess, sys

CREATE_NO_WINDOW = 0x08000000   # Windows: don't flash a console window

_WIN_TOAST_PS = r"""
$ErrorActionPreference = 'Stop'
try {
  [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime] | Out-Null
  $t = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
  $x = $t.GetElementsByTagName('text')
  $x.Item(0).AppendChild($t.CreateTextNode($env:HAL_TITLE)) | Out-Null
  $x.Item(1).AppendChild($t.CreateTextNode($env:HAL_BODY))  | Out-Null
  $toast = [Windows.UI.Notifications.ToastNotification]::new($t)
  [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('Claude Session HUD').Show($toast)
} catch { }
"""


def _run(cmd, env=None):
    """Fire-and-forget a notification subprocess; never raise."""
    try:
        subprocess.Popen(cmd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=(CREATE_NO_WINDOW if os.name == "nt" else 0))
    except Exception:
        pass


def _notify_macos(title, body):
    # Escape for AppleScript string literals.
    def esc(s): return s.replace("\\", "\\\\").replace('"', '\\"')
    script = f'display notification "{esc(body)}" with title "{esc(title)}"'
    _run(["osascript", "-e", script])


def _notify_linux(title, body):
    from shutil import which
    if which("notify-send"):
        _run(["notify-send", "-a", "Claude Session HUD", title, body])
    elif which("zenity"):
        _run(["zenity", "--notification", f"--text={title}: {body}"])


def _notify_windows(title, body):
    env = dict(os.environ, HAL_TITLE=title, HAL_BODY=body)
    _run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", _WIN_TOAST_PS], env=env)


def notify(title, body):
    """Show a native desktop notification. Best-effort, never raises."""
    title = " ".join(str(title or "").split()) or "Claude Session HUD"
    body = " ".join(str(body or "").split())
    system = platform.system()
    try:
        if system == "Darwin":
            _notify_macos(title, body)
        elif system == "Windows":
            _notify_windows(title, body)
        else:
            _notify_linux(title, body)
    except Exception:
        pass


if __name__ == "__main__":
    # Manual test: python hal_notify.py "Title" "Body"
    notify(sys.argv[1] if len(sys.argv) > 1 else "Claude Session HUD",
           sys.argv[2] if len(sys.argv) > 2 else "A session is waiting for you.")
