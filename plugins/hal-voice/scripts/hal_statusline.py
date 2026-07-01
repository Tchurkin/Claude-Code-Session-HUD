#!/usr/bin/env python3
"""
Statusline: tag each chat with its assigned HAL color.

Claude Code runs this for every session and pipes the session JSON on stdin; we print a
one-line status tinted with the SAME color that session's popups use (from
``hal_common.session_color``), so the chat visibly carries its color. This is the
closest thing to "color the chat tab" that a hook can do — the editor tab itself isn't
controllable from outside the VS Code extension.

Output: a solid color chip, a HAL eye + the project folder in the chat color, then the
model name in grey. Uses 24-bit ("truecolor") ANSI, which the Claude Code status line
renders.
"""
import json, os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hal_common as hc

RESET = "\x1b[0m"


def main():
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except Exception:
        data = {}

    r, g, b = hc.session_color(data.get("session_id"))
    fg   = f"\x1b[38;2;{r};{g};{b}m"
    chip = f"\x1b[48;2;{r};{g};{b}m  {RESET}"      # solid color block
    grey = "\x1b[38;2;150;150;150m"

    ws    = data.get("workspace") or {}
    cwd   = ws.get("current_dir") or data.get("cwd") or os.getcwd()
    proj  = os.path.basename(str(cwd).rstrip("/\\")) or str(cwd)
    model = (data.get("model") or {}).get("display_name") or ""

    parts = [f"{chip} {fg}● {proj}{RESET}"]    # ● + project, in the chat color
    if model:
        parts.append(f"{grey}{model}{RESET}")
    if hc.is_muted():
        parts.append(f"{grey}(muted){RESET}")
    out = "  ".join(parts)
    # Windows stdout defaults to cp1252, which can't encode the ● glyph - emit UTF-8 bytes.
    try:
        sys.stdout.buffer.write(out.encode("utf-8"))
    except Exception:
        sys.stdout.write(out)


if __name__ == "__main__":
    main()
