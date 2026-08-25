"""A hook firing while you've clicked elsewhere must not re-point a tab at that other window.

Also: a chat that cds into a subfolder keeps its project identity (its tab must not become
"Scripts"). The desktop here is three imaginary VS Code windows - nothing on screen is read.
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import Sandbox, check

import hal_badge as hb          # the harness put scripts/ on sys.path

SID = "cap11111-2222-3333-4444-666666666666"

with Sandbox() as sb:
    # ── a desktop of three VS Code windows, none of them real ─────────────────
    #  MINE        the window showing THIS chat, opened on a multi-root workspace, so its title
    #              never names the project folder - only the chat's own (truncated) title.
    #  OTHER       an unrelated chat in an unrelated repo: what you clicked into while the hook
    #              was still in flight.
    #  FOLDER_WIN  a plain window that does name the project folder.
    MINE, OTHER, FOLDER_WIN = 0x111, 0x222, 0x333
    CWD   = os.path.join(sb.root, "work", "TVC PID Research")
    PROJ  = os.path.basename(CWD)
    TITLE = "Prepare sensor fusion and firmware for landing test"
    TITLES = {MINE:       "Prepare sensor fusion an\u2026 - Untitled (Workspace) - Visual Studio Code",
              OTHER:      "Some other chat\u2026 - Unrelated Repo - Visual Studio Code",
              FOLDER_WIN: "main.py - %s - Visual Studio Code" % PROJ}
    FG = [0]                                     # what the user is focused on right now
    sb._set(hb, "_foreground_hwnd", lambda: FG[0])
    sb._set(hb, "_window_title", lambda h: TITLES.get(h, ""))
    hb._find_chat_windows = lambda cwd: [FOLDER_WIN] if PROJ in str(cwd) else []

    # ── 1. which window a capture is allowed to trust ─────────────────────────
    FG[0] = MINE                     # you typed in the window that is showing this chat
    check(hb._capture_hwnd(CWD, TITLE) == MINE, "the window showing this chat is the right answer")

    FG[0] = OTHER                    # async hook: by now you have clicked into an unrelated window
    got = hb._capture_hwnd(CWD, TITLE)
    check(got != OTHER, "an unrelated focused window must never be captured")
    check(got == FOLDER_WIN, "it falls back to the window that names the project folder")
    print("focused elsewhere ->", hex(got), "(falls back to the folder window, not the focused one)")

    FG[0] = 0                        # nothing focused
    check(hb._capture_hwnd(CWD, TITLE) == FOLDER_WIN, "no focus -> the folder window")

    hb._find_chat_windows = lambda cwd: []       # multi-root: no window names the folder
    FG[0] = OTHER
    check(hb._capture_hwnd(CWD, TITLE) == 0, "no plausible window -> keep whatever the tab already had")
    FG[0] = MINE
    check(hb._capture_hwnd(CWD, TITLE) == MINE, "...but the window showing us still wins")
    print("1. capture trust      focused-and-showing-us=0x%x  focused-elsewhere=0x%x  unbindable=0"
          % (MINE, FOLDER_WIN))

    # ── 2. the same rule through a real hook, on a real tab ───────────────────
    # The unit calls above are what touch(capture_hwnd=True) leans on; this is the bug as it was
    # actually reported - a tab that had been correctly bound getting re-pointed by a late hook.
    sb.add_session(SID, CWD)
    hb._find_chat_windows = lambda cwd: [FOLDER_WIN] if PROJ in str(cwd) else []
    FG[0] = MINE
    hb.touch(SID, cwd=CWD, capture_hwnd=True, state="working")
    st = sb.state(SID)
    check(st["hwnd"] == FOLDER_WIN, "before the chat has a known title, only the folder window is plausible")

    st["title"] = TITLE                          # the reconciler learns the chat's own title
    hb._write_state(SID, st)
    hb.touch(SID, cwd=CWD, capture_hwnd=True, state="working")
    check(sb.state(SID)["hwnd"] == MINE, "the window showing this chat by name wins over the folder window")

    hb._find_chat_windows = lambda cwd: []       # nothing on screen names the folder any more
    FG[0] = OTHER                                # ...and the late hook fires while you are elsewhere
    hb.touch(SID, cwd=CWD, capture_hwnd=True, state="working")
    st = sb.state(SID)
    check(st["hwnd"] != OTHER, "a late hook must never hand the tab the window you clicked into")
    check(st["hwnd"] == MINE, "it keeps the binding it already had")
    print("2. late hook          tab stayed on 0x%x while 0x%x was focused" % (st["hwnd"], OTHER))

    # ── 3. a chat that cds into a subfolder keeps its project identity ────────
    ROOT = os.path.join(sb.root, "work", "Claude-Code-Session-HUD-main")
    SUB  = os.path.join(ROOT, "plugins", "hal-voice", "scripts")
    check(hb._stable_cwd(SUB, ROOT) == ROOT, "a subfolder is drift, not a move")
    check(hb._stable_cwd(ROOT, ROOT) == ROOT, "standing still is not a move")
    check(hb._stable_cwd(ROOT, SUB) == ROOT, "the real root wins when the reconciler supplies it")
    ELSEWHERE = os.path.join(sb.root, "other", "project")
    check(hb._stable_cwd(ELSEWHERE, ROOT) == ELSEWHERE, "a different project is a move")
    check(hb._stable_cwd(ROOT + "-old", ROOT) == ROOT + "-old", "a name that merely starts the same is a move")
    check(hb._stable_cwd("", ROOT) == ROOT and hb._stable_cwd(ROOT, "") == ROOT, "empties fall back")
    check(hb._proj_label(SUB) == "Scripts", "...which is exactly the name a drifting tab would take")
    check(hb._proj_label(hb._stable_cwd(SUB, ROOT)) == "Claude Code Session HUD",
          "the tab keeps the project's name, not the subfolder's")
    print("3. cwd drift          %s -> %s" % (os.path.basename(SUB), hb._proj_label(hb._stable_cwd(SUB, ROOT))))

    # ── 4. the same, on the live tab: cd-ing must not rename it ───────────────
    DEEP = os.path.join(CWD, "firmware", "src")
    before = sb.state(SID)
    check(before["label"] == PROJ and before["cwd"] == CWD, "the tab is named for its project")
    hb.touch(SID, cwd=DEEP, capture_hwnd=False, state="working")
    st = sb.state(SID)
    check(st["cwd"] == CWD, "the tab's cwd stays pinned to the project root")
    check(st["proj"] == PROJ and st["label"] == PROJ,
          "and its name stays the project's, not '%s'" % hb._proj_label(DEEP))
    print("4. tab after cd       label=%r cwd=%s" % (st["label"], os.path.basename(st["cwd"])))

print("OK - a capture is only trusted when the focused window is plausibly this chat's, and a chat "
      "keeps its project identity however far it cds inside it")
