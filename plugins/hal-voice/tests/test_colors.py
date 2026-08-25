"""Every tab colour has to be readable as text on the chip's near-black background.

Pure colour maths, plus one scenario that walks the same numbers through the real badge path in a
sandbox: eight synthetic open chats, each claiming a slot, each ending up with a legible accent no
other chat shares. Nothing here depends on what is open on the machine running it.
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import Sandbox, check

import colorsys                       # noqa: E402
import hal_common as hc               # noqa: E402
import hal_badge as hb                # noqa: E402

BG_LIT, BG_DIM = hc._CHIP_BG, (17, 17, 17)
SLOTS = 24


def _dist(x, y):
    """Channel distance between two colours, 0..765."""
    return sum(abs(a - b) for a, b in zip(x, y))


# ── 1. every slot clears the WCAG AA floor on the chip's lightest background ──────────────────
worst = 99.0
for slot in range(SLOTS):
    c = hc.slot_color(slot)
    lit, dim = hc.contrast_ratio(c, BG_LIT), hc.contrast_ratio(c, BG_DIM)
    worst = min(worst, lit)
    check(lit >= hc._MIN_CONTRAST - 0.01,
          'slot %d %s only %.1f:1 on the lit chip' % (slot, c, lit))
    check(dim >= lit, 'the dim chip is always the easier background (slot %d: %.1f vs %.1f)'
                      % (slot, dim, lit))
print('%d slots, worst contrast on the lit chip: %.1f:1 (floor %.1f)'
      % (SLOTS, worst, hc._MIN_CONTRAST))

# ── 2. hues that were already bright must not be dulled or lightened needlessly ───────────────
untouched = 0
for slot in range(SLOTS):
    hue = ((hc._HUE_START + slot * hc._GOLDEN) % 360.0) / 360.0
    r, g, b = colorsys.hsv_to_rgb(hue, hc._SAT, hc._VAL)
    full = (round(r * 255), round(g * 255), round(b * 255))
    if hc.contrast_ratio(full, BG_LIT) >= hc._MIN_CONTRAST:
        check(hc.slot_color(slot) == full, 'slot %d was already legible; leave it alone' % slot)
        untouched += 1
print('%d of %d slots were legible at full saturation and came back untouched'
      % (untouched, SLOTS))

# ── 3. the first eight slots stay tellable apart ──────────────────────────────────────────────
first8 = [hc.slot_color(i) for i in range(8)]
check(len(set(first8)) == 8, 'two of the first 8 slots collapsed onto one colour: %s' % (first8,))
gaps = [_dist(x, y) for i, x in enumerate(first8) for y in first8[i + 1:]]
print('closest pair of the first 8 slots: %d/765 channel distance' % min(gaps))
check(min(gaps) > 60, 'colours must stay tellable apart (closest pair %d/765)' % min(gaps))

# ── 4. and that is what eight open chats actually get, through the real badge path ────────────
with Sandbox() as sb:
    sids = ['%08x-cafe-4000-8000-00000000000%d' % (0xc0100000 + i, i) for i in range(8)]
    for i, sid in enumerate(sids):
        cwd = os.path.join(sb.root, 'proj%d' % i)
        os.makedirs(cwd, exist_ok=True)
        sb.add_session(sid, cwd, pid=os.getpid() + i)
        hb.touch(sid, cwd=cwd, state='working')
    check(len(sb.tabs()) == 8, 'expected 8 tabs, got %s' % (sb.tabs(),))
    slots = sorted(int(sb.state(s)['slot']) for s in sids)
    check(slots == list(range(8)), 'eight open chats must hold slots 0..7, got %s' % (slots,))
    live = []
    for sid in sids:
        st = hb._read_state(sid)
        col = tuple(st['color'])
        check(col == hc.slot_color(st['slot']),
              'tab %s painted %s, not its slot colour %s' % (sid[:8], col, hc.slot_color(st['slot'])))
        check(hc.contrast_ratio(col, BG_LIT) >= hc._MIN_CONTRAST - 0.01,
              'tab %s accent %s is unreadable on the lit chip' % (sid[:8], col))
        live.append(col)
    check(len(set(live)) == 8, 'two open chats share an accent: %s' % (live,))
    check(set(live) == set(first8), 'open chats got colours outside the first 8 slots')
    near = min(_dist(x, y) for i, x in enumerate(live) for y in live[i + 1:])
    check(near > 60, 'two open chats are too close to tell apart (%d/765)' % near)
    print('8 open chats -> slots %s, all distinct, closest pair %d/765' % (slots, near))

print('OK - every slot is legible, and the ones that already were are untouched')
