"""Window binding: does each tab point at the window its chat actually lives in?

The original of this suite ran against the developer's own machine - whatever chats happened to be
open, whatever VS Code windows were on screen. Here the Sandbox builds the whole world instead
(scratch state dir, fake transcripts, a fake desktop, fake extension reports), so the run is
identical on a laptop with twelve chats open and on a CI box with none.
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import Sandbox, check

import json                                                            # noqa: E402
import hal_badge as hb                                                 # noqa: E402
import hal_sessions as hs                                              # noqa: E402

# The genuine reader of the extension's window reports, captured before the Sandbox stubs it out -
# scenario 10 needs the real staleness filter, not a stand-in for it.
REAL_REPORTED = hs._reported_windows

W_MULTI, W_TVC, W_TVC2, W_MULTI2 = 0x10466, 0x10486, 0x20000, 0x444

FUSION_T = 'Prepare sensor fusion and firmware for landing test'
AUDIT_T  = 'Audit rocket control project claims for STS submission'
FULL     = 'Analyze college admission essays for patterns and techniques'
ELL      = '…'                       # the ellipsis VS Code truncates titles and labels with

# The fake desktop, re-pointed by each scenario. The stubs below read these at call time.
TITLES, WINDOWS, LIVE_WINDOWS = {}, {}, set()


def sess(sid, cwd, started=0):
    """A live-chat record in the shape ``discover()`` hands to the binder."""
    return {'sid': sid, 'cwd': cwd, 'started': started, 'name': '', 'entry': ''}


def bind(live):
    r = hs._bind_windows(live)
    return {s['sid'][:8]: r[s['sid']][0] for s in live}


def full_bind(live):
    r = hs._bind_windows(live)
    return {s['sid'][:8]: r[s['sid']] for s in live}


def hexes(d):
    return {k: hex(v) for k, v in d.items()}


def ascii_(s):
    """Printable anywhere: the console's encoding is one more machine dependency to shed."""
    return str(s).encode('ascii', 'backslashreplace').decode('ascii')


with Sandbox() as sb:
    # -- the fake desktop -------------------------------------------------------------------
    hb._window_chat_titles = lambda: dict(TITLES)
    hb._find_chat_windows  = lambda cwd: sorted(WINDOWS.get(str(cwd).rstrip('/\\').lower(), []))
    hb._is_vscode_window   = lambda h: h in LIVE_WINDOWS

    CWD_TVC   = os.path.join(sb.root, 'work', 'TVC PID Research')
    CWD_GHOST = os.path.join(sb.root, 'nowhere', 'ghost')
    os.makedirs(CWD_TVC, exist_ok=True)

    def titles_of(*pairs):
        """Give each chat exactly the title this scenario needs, the way a real chat gets one: it is
        written in its transcript and read back out by ``hs._chat_title``. '' means untitled."""
        for s, title in pairs:
            sb.add_transcript(s['sid'], s['cwd'], ['do the work'], ai_title=(title or None))
            if hb._read_state(s['sid']):
                hb.update_state(s['sid'], title='', title_ts=0)   # drop the once-a-minute cache

    # -- 1. the reported bug: a chat living in a multi-root window whose title never names the
    #       folder, while a sibling chat's window does. Folder matching sent both to the sibling's.
    fusion = sess('aaaaaaaa-fusion', CWD_TVC, 1)
    audit  = sess('bbbbbbbb-audit',  CWD_TVC, 2)
    WINDOWS[CWD_TVC.lower()] = [W_TVC]                    # only this one advertises the folder
    LIVE_WINDOWS.update({W_MULTI, W_TVC})
    TITLES = {W_MULTI: 'prepare sensor fusion an', W_TVC: 'audit rocket control pro'}
    titles_of((fusion, FUSION_T), (audit, AUDIT_T))
    b = bind([fusion, audit])
    print('1.  multi-root window      ', hexes(b))
    check(b['aaaaaaaa'] == W_MULTI, 'the chat the multi-root window is showing must bind to it')
    check(b['bbbbbbbb'] == W_TVC, 'the sibling keeps the window that names its folder')

    # -- 2. the binding sticks once made: the user clicks onto a source file, so the window stops
    #       advertising the chat. It must not drift back to the folder-name window.
    for s in (fusion, audit):
        hb.touch(s['sid'], s['cwd'], hwnd=b[s['sid'][:8]], name='X', label_src='kw')
    TITLES = {W_MULTI: 'main.py', W_TVC: 'audit rocket control pro'}
    b = bind([fusion, audit])
    print('2.  window shows a file    ', hexes(b))
    check(b['aaaaaaaa'] == W_MULTI, 'a known binding survives the window renaming itself')

    # -- 3. the chat moves: it is now the active tab in the OTHER window. Evidence beats stickiness.
    TITLES = {W_MULTI: 'main.py', W_TVC: 'prepare sensor fusion an'}
    b = bind([fusion, audit])
    print('3.  chat moved windows     ', hexes(b))
    check(b['aaaaaaaa'] == W_TVC, 'live evidence must override a stale binding')

    # -- 4. its window is gone: fall back rather than point at a dead handle
    TITLES = {}
    LIVE_WINDOWS.discard(W_MULTI)
    hb.touch(fusion['sid'], fusion['cwd'], hwnd=W_MULTI, name='X', label_src='kw')
    b = bind([fusion, audit])
    print('4.  window closed          ', hexes(b))
    check(b['aaaaaaaa'] == W_TVC, 'a dead handle is dropped, not kept')

    # -- 5. no evidence at all, two windows on one folder: spread them instead of stacking
    LIVE_WINDOWS.clear()
    TITLES = {}
    WINDOWS[CWD_TVC.lower()] = [W_TVC, W_TVC2]
    for s in (fusion, audit):
        hb.retire(hb._sid8(s['sid']))
    titles_of((fusion, ''), (audit, ''))                  # nothing has named itself
    b = bind([fusion, audit])
    print('5.  no evidence, 2 windows ', hexes(b))
    check(b['aaaaaaaa'] != b['bbbbbbbb'], 'unidentified chats spread across the available windows')

    # -- 6. chats genuinely sharing one window both point at it
    WINDOWS[CWD_TVC.lower()] = [W_TVC]
    LIVE_WINDOWS.add(W_TVC)
    TITLES = {W_TVC: 'audit rocket control pro'}
    titles_of((fusion, ''), (audit, AUDIT_T))
    for s in (fusion, audit):
        hb.retire(hb._sid8(s['sid']))
    b = bind([fusion, audit])
    print('6.  one shared window      ', hexes(b))
    check(b['aaaaaaaa'] == b['bbbbbbbb'] == W_TVC,
          'sharing a window is a real answer, not a conflict')

    # only the chat the window is actually displaying counts as "the tab you're on"
    show = {k: v[1] for k, v in full_bind([fusion, audit]).items()}
    print('    showing:                ', show)
    check(show['bbbbbbbb'] is True and show['aaaaaaaa'] is False,
          'only the displayed chat is lit: %r' % show)
    TITLES = {}                             # window shows a file: nobody identified -> all lit
    show = {k: v[1] for k, v in full_bind([fusion, audit]).items()}
    check(all(show.values()), 'with no evidence the highlight behaves as before, not dark')

    # -- 7. a chat in a folder no window advertises, with nothing else to go on: no window, not a
    #       wrong one
    ghost = sess('cccccccc-orphan', CWD_GHOST, 3)
    b = bind([ghost])
    print('7.  no window at all       ', hexes(b))
    check(b['cccccccc'] == 0, 'better no target than the wrong window')

    # -- 8. the companion extension reports what each window actually holds. Now a chat sitting in a
    #       BACKGROUND tab is placeable too - something a window title can never tell us.
    titles_of((fusion, FUSION_T), (audit, AUDIT_T))
    WINDOWS[CWD_TVC.lower()] = [W_TVC]                    # only the other window advertises the folder
    LIVE_WINDOWS.clear()
    LIVE_WINDOWS.update({W_TVC, W_MULTI2})
    for s in (fusion, audit):
        hb.retire(hb._sid8(s['sid']))

    # both chats live in a window that is currently showing a source file
    TITLES = {W_MULTI2: 'main.py', W_TVC: 'something else entirely'}
    sb.report_window('main.py', [FUSION_T, AUDIT_T, 'main.py'], folder=CWD_TVC)
    r = full_bind([fusion, audit])
    print('8.  reported, file in front', {k: (hex(v[0]), v[1]) for k, v in r.items()})
    check(r['aaaaaaaa'][0] == r['bbbbbbbb'][0] == W_MULTI2, 'both chats are where the window says')
    check(not r['aaaaaaaa'][1] and not r['bbbbbbbb'][1], 'a window on a source file lights nothing')

    # -- 9. now the window switches to one of them
    TITLES = {W_MULTI2: 'prepare sensor fusion an', W_TVC: 'something else entirely'}
    sb.report_window(FUSION_T, [FUSION_T, AUDIT_T, 'main.py'], folder=CWD_TVC)
    r = full_bind([fusion, audit])
    print('9.  reported, chat in front', {k: (hex(v[0]), v[1]) for k, v in r.items()})
    check(r['aaaaaaaa'][:2] == (W_MULTI2, True), 'the chat in front is the one you are on')
    check(r['bbbbbbbb'][:2] == (W_MULTI2, False),
          'its neighbour is in the same window but not in front')

    # -- 10. a report that has gone stale must not outvote what we can see. Written as a real file in
    #        the windows dir and read by the real reader, so the staleness filter itself is on trial.
    hs._reported_windows = REAL_REPORTED
    for f in os.listdir(hs.WINDOWS_DIR):
        os.remove(os.path.join(hs.WINDOWS_DIR, f))
    rep_path = os.path.join(hs.WINDOWS_DIR, 'win-1234.json')

    # A pinned clock, so "fresh" and "stale" are decided by the ts written into the report and by
    # WINDOW_STALE_MS - never by how long this test took to reach this line. The staleness
    # arithmetic and the reaping still run for real; only the wall clock stops moving.
    REAL_NOW, NOW = hs._now, hs._now()
    hs._now = lambda: NOW

    # The report claims fusion sits in the window whose ACTIVE tab is the audit chat - i.e. it
    # points somewhere the window titles do not. That disagreement is the whole point: were the
    # stale report still counted, fusion would land in W_TVC, so the check below can actually fail.
    TITLES = {W_MULTI2: 'prepare sensor fusion an', W_TVC: 'audit rocket control pro'}

    def write_report(ts):
        with open(rep_path, 'w', encoding='utf-8') as f:
            json.dump({'ts': ts, 'pid': 1234, 'folder': CWD_TVC,
                       'active': AUDIT_T, 'tabs': [FUSION_T, AUDIT_T]}, f)

    write_report(hs._now())                               # fresh: the real reader does hand it over
    check(len(REAL_REPORTED()) == 1, 'a fresh report is read from the windows dir')
    fresh = full_bind([fusion, audit])
    check(fresh['aaaaaaaa'][0] == W_TVC,
          'while the report is fresh it does place fusion in the window it names')
    write_report(hs._now() - hs.WINDOW_STALE_MS - 1000)   # ...and now that window has gone quiet
    r = full_bind([fusion, audit])
    print('10. stale report           ', {k: hex(v[0]) for k, v in r.items()},
          '| report reaped:', not os.path.exists(rep_path))
    check(r['aaaaaaaa'][0] == W_MULTI2,
          'falls back to the window title, which still shows this chat')
    check(not os.path.exists(rep_path), "a dead window's report is cleaned up, not left to rot")
    hs._now = REAL_NOW

    # -- 11. VS Code truncates TAB LABELS too, not just window titles ("Analyze college admissio..."),
    #        at a different length from the title. Matching has to work on the stem, from either side.
    check(hb._same_chat(FULL, 'Analyze college admissio' + ELL),
          'a truncated tab label still names this chat')
    check(hb._same_chat('Analyze college admissio' + ELL, FULL), 'and the comparison is symmetric')
    check(hb._same_chat(FULL, FULL), 'a chat is itself')
    check(not hb._same_chat(FULL, 'Explore college options ' + ELL), 'different chats stay different')
    check(not hb._same_chat(FULL, 'Analyze' + ELL), 'too short a stem is not evidence')
    check(not hb._same_chat(FULL, ''), 'an untitled chat matches nothing')

    # a chat placed by a truncated label gets the exact label recorded, so the click asks for it
    # verbatim
    LABEL = 'Analyze college admissio' + ELL
    hb.retire(hb._sid8(fusion['sid']))
    titles_of((fusion, FULL))
    TITLES = {W_MULTI2: 'analyze college admissio', W_TVC: 'x'}
    sb.report_window(LABEL, ['main.py', LABEL], folder=CWD_TVC)
    hb.touch(fusion['sid'], fusion['cwd'], hwnd=0, name='X', label_src='kw')
    r = hs._bind_windows([fusion])[fusion['sid']]
    print('11. truncated tab label    ', hex(r[0]), '| showing:', r[1],
          '| label returned:', ascii_(repr(r[2])))
    check(r[:2] == (W_MULTI2, True),
          'a chat named only by a truncated label is still placed, and is in front')
    check(r[2] == LABEL, 'the exact label is what a click will ask for')

print('OK - tabs bind to the window their chat is really in: the window showing a chat beats the '
      'folder name, a binding sticks until better evidence arrives, dead handles are dropped, '
      'unidentified chats spread across free windows but happily share one, an unplaceable chat '
      'gets no window at all, extension reports place background tabs and expire gracefully, and '
      'truncated tab labels resolve to the exact label a click asks for')
