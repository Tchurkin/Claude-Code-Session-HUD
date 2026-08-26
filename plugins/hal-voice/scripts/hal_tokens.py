#!/usr/bin/env python3
"""
How fast you are actually spending tokens, from the transcripts.

The plan's own usage endpoint gives a percentage, and a percentage only moves in whole points - so
between ticks it tells you nothing, and the burn rate fitted to it needs five minutes before it says
anything at all. The transcripts have the real numbers, per message, with timestamps, and they are
already on disk. This tails them and turns them into a rate.

Three things had to be right or the number is fiction:

* Assistant records repeat their usage block once per content block. On a live transcript here that
  is 1779 records for 840 actual API calls - summing naively over-counts by 2.1x. Deduplicated on
  ``message.id``.
* Sub-agent spend lives in a completely separate file tree, and on a busy chat it is larger than the
  main transcript. Walking the projects directory recursively picks both up without needing to know
  the layout.
* No raw token total tracks the plan's utilization. Output tokens alone are off by more than 10x as
  a predictor, because a turn that emits 1,200 tokens can re-send half a million cached ones. A
  cost-weighted sum tracks it closely, so that is the headline: weighted so a cache read counts a
  tenth of fresh input and an output token counts five, which is the shape of the price list.
"""
import glob, json, os, time

import hal_common as hc

CLAUDE_DIR   = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(hc.HOME, ".claude")
PROJECTS_DIR = os.path.join(CLAUDE_DIR, "projects")
CACHE        = os.path.join(hc.DATA_DIR, "tokens.json")

REFRESH_MS   = 5000            # the daemon calls this often; the work is gated to here
RESCAN_MS    = 30000           # how often to re-list the projects tree rather than the hot files
WINDOW_MS    = 10 * 60 * 1000  # rate window: shorter than the utilization fit, being unquantized
HOT_MS       = 20 * 60 * 1000  # a file untouched this long is not worth stat-ing every pass
MAX_IDS      = 4000            # recent message ids kept to dedup across a chunk boundary
MAX_CHUNK    = 4 * 1024 * 1024 # bytes read from one file in one pass (a cold start could be huge)
TOP_CHATS    = 4               # how many chats the panel has room to name

# Relative to one Opus input token. Output is 5x input on every current model, so it factors out of
# the per-model scalar; a cache read is a tenth, a 5-minute cache write 1.25, an hour one 2.
W_OUT, W_READ, W_5M, W_1H = 5.0, 0.10, 1.25, 2.0
MODEL_W = (("opus", 1.0), ("sonnet", 0.6), ("haiku", 0.2))


def _sid_of(path):
    """Which chat a transcript belongs to, from its path.

    A chat's own transcript is ``<slug>/<sessionId>.jsonl``; anything its sub-agents ran lives under
    ``<slug>/<sessionId>/subagents/...``. So the session id is either the file's stem or the
    directory just above "subagents" - which is how a sub-agent's spend gets billed to the chat that
    started it rather than floating free."""
    parts = str(path).replace("\\", "/").split("/")
    if "subagents" in parts:
        i = parts.index("subagents")
        raw = parts[i - 1] if i >= 1 else ""
    else:
        raw = os.path.splitext(parts[-1])[0] if parts else ""
    keep = "".join(ch for ch in raw[:8] if ch.isalnum())      # matches hal_badge._sid8
    return keep or "?"


def _model_weight(name):
    n = (name or "").lower()
    for key, w in MODEL_W:
        if key in n:
            return w
    return 1.0                 # unknown model: assume the dear one rather than flatter the number


def _iso_ms(s):
    try:
        from datetime import datetime, timezone
        t = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return int(t.timestamp() * 1000)
    except Exception:
        return 0


def weigh(usage, model):
    """(weighted, output, raw_input) for one usage block.

    `thinking_tokens` is deliberately not added: it is a subset of output_tokens, already counted.
    `iterations` likewise duplicates the top level."""
    def g(k, d=0):
        try:
            return float(usage.get(k) or d)
        except Exception:
            return d
    out  = g("output_tokens")
    read = g("cache_read_input_tokens")
    inp  = g("input_tokens")
    cc = usage.get("cache_creation")
    if isinstance(cc, dict):
        m5 = float(cc.get("ephemeral_5m_input_tokens") or 0)
        h1 = float(cc.get("ephemeral_1h_input_tokens") or 0)
    else:
        m5, h1 = g("cache_creation_input_tokens"), 0.0
    weighted = _model_weight(model) * (inp + W_5M * m5 + W_1H * h1 + W_READ * read + W_OUT * out)
    return (weighted, out, inp + m5 + h1 + read)


def _read_new(path, offset):
    """Whole lines added since `offset`. Returns (lines, new_offset).

    Stops at the last newline so a record still being written is left for next time, and starts over
    if the file has shrunk - that is a new file at a path we have seen, not a rewind."""
    try:
        size = os.path.getsize(path)
    except Exception:
        return ([], offset)
    if size < offset:
        offset = 0                                  # truncated or replaced
    if size <= offset:
        return ([], offset)
    start = max(offset, size - MAX_CHUNK)           # a cold start must not read a 50MB transcript
    try:
        with open(path, "rb") as f:
            f.seek(start)
            buf = f.read(size - start)
    except Exception:
        return ([], offset)
    cut = buf.rfind(b"\n")
    if cut < 0:
        return ([], offset)                         # no complete line yet
    text = buf[:cut].decode("utf-8", "replace")
    if start > offset:
        text = text.split("\n", 1)[-1]              # we skipped in: drop the partial first line
    # Offsets stay in bytes, so normalising the line endings here costs nothing and means a
    # transcript written with CRLF does not arrive with a stray return glued to every record.
    return ([ln.rstrip("\r") for ln in text.split("\n")], start + cut + 1)


def read():
    try:
        with open(CACHE, encoding="utf-8-sig") as f:
            d = json.load(f)
            return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _publish(d):
    try:
        os.makedirs(hc.DATA_DIR, exist_ok=True)
        tmp = CACHE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f)
        os.replace(tmp, CACHE)
    except Exception:
        pass


def _rate(samples, now, window=WINDOW_MS):
    """Per-minute rates over the window. Divides by the window, not by the span between the first
    and last sample: a burst three minutes ago followed by silence IS a lower rate now, and dividing
    by the span would keep reporting the burst's rate forever."""
    lo = now - window
    w = o = c = 0.0
    for s in samples:
        if s[0] >= lo:
            w += s[1]; o += s[2]; c += s[3]
    mins = window / 60000.0
    return (w / mins, o / mins, c / mins)


def by_chat(samples, now, window=WINDOW_MS, top=TOP_CHATS):
    """Weighted tokens a minute, per chat, biggest first.

    The one thing the HUD knew separately about tabs and about usage and never joined up: which of
    the chats you have open is the one eating the window."""
    lo = now - window
    per = {}
    for s in samples:
        if len(s) >= 5 and s[0] >= lo:
            per[s[4]] = per.get(s[4], 0.0) + float(s[1])
    mins = window / 60000.0
    rows = sorted(((sid, v / mins) for sid, v in per.items()), key=lambda r: -r[1])
    return [[sid, int(round(v))] for sid, v in rows[:top] if v >= 1]


def refresh(force=False):
    """Tail every recently-written transcript and republish the rates. Cheap enough for a 5s tick."""
    cur = read()
    now = int(time.time() * 1000)
    if not force and now - float(cur.get("ts") or 0) < REFRESH_MS:
        return cur

    offsets = dict(cur.get("offsets") or {})
    samples = [s for s in (cur.get("samples") or []) if isinstance(s, list) and len(s) >= 4
               and s[0] >= now - WINDOW_MS]
    total = float(cur.get("total") or 0.0)      # monotonic: what the live percentage extrapolates on
    seen = list(cur.get("ids") or [])
    seen_set = set(seen)
    files = list(cur.get("hot") or [])
    scanned = float(cur.get("scan_ts") or 0)

    if force or now - scanned >= RESCAN_MS or not files:
        # One recursive listing picks up main transcripts and the subagent trees alike. Only files
        # written recently can contain anything inside the window, and there are ~700 of them.
        scanned = now
        try:
            files = [p for p in glob.glob(os.path.join(PROJECTS_DIR, "**", "*.jsonl"), recursive=True)
                     if (now / 1000.0) - os.path.getmtime(p) < HOT_MS / 1000.0]
        except Exception:
            files = []

    for path in files:
        sid = _sid_of(path)
        lines, offsets[path] = _read_new(path, int(offsets.get(path) or 0))
        for line in lines:
            if '"usage"' not in line:               # cheap reject: most records are not assistant turns
                continue
            try:
                e = json.loads(line)
            except Exception:
                continue
            m = e.get("message")
            if not isinstance(m, dict):
                continue
            u = m.get("usage")
            if not isinstance(u, dict):
                continue
            mid = m.get("id")
            if mid:
                if mid in seen_set:                 # the same call, repeated once per content block
                    continue
                seen_set.add(mid); seen.append(mid)
            ts = _iso_ms(e.get("timestamp")) or now
            w, o, c = weigh(u, m.get("model"))
            if w > 0:
                samples.append([ts, round(w, 1), o, c, sid])
                # Only spend from inside the window counts toward the running total. A cold start
                # tails up to four megabytes of existing transcript, and every one of those records
                # is history, not something that just happened - counting them would inject a
                # phantom of hundreds of thousands of tokens with no matching movement in the plan's
                # own figure, which is exactly the pair that would ruin the calibration.
                if ts >= now - WINDOW_MS:
                    total += w

    samples = [s for s in samples if s[0] >= now - WINDOW_MS]
    samples.sort(key=lambda s: s[0])
    if len(seen) > MAX_IDS:
        seen = seen[-MAX_IDS:]
        seen_set = set(seen)
    # Forget files that have gone quiet, so `offsets` cannot grow without bound across a long day.
    live = set(files)
    offsets = {k: v for k, v in offsets.items() if k in live}

    tpm, opm, cpm = _rate(samples, now)
    out = {"ts": now, "scan_ts": scanned, "hot": files, "offsets": offsets,
           "samples": samples, "ids": seen, "total": round(total, 1),
           "tpm": int(round(tpm)), "opm": int(round(opm)), "cpm": int(round(cpm)),
           "n": len(samples), "by_chat": by_chat(samples, now)}
    _publish(out)
    return out


if __name__ == "__main__":
    import sys
    t0 = time.time()
    u = refresh(force="--no-scan" not in sys.argv)
    print("weighted %s/min   output %s/min   raw in %s/min   (%d calls in the last %d min, %.0f ms)"
          % ("{:,}".format(u.get("tpm", 0)), "{:,}".format(u.get("opm", 0)),
             "{:,}".format(u.get("cpm", 0)), u.get("n", 0), WINDOW_MS / 60000,
             (time.time() - t0) * 1000))
    print("tailing %d recently-written transcripts" % len(u.get("hot") or []))
    for sid, v in (u.get("by_chat") or []):
        print("   %-10s %s weighted/min" % (sid, "{:,}".format(v)))
