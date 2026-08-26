"""Tokens a minute, read out of the transcripts.

The plan's own figure only moves in whole percentage points, so between ticks it says nothing. The
transcripts have the real numbers with timestamps and are already on disk. Three things had to be
right or the figure is fiction, and all three are load-bearing enough to pin here:

* assistant records repeat their usage block once per content block - 2.1x over-count if summed
* no raw token total tracks the plan's utilization; a cost-weighted one does
* the files are megabytes and grow all day, so they are tailed, never re-read

Builds its own transcripts in a scratch directory. Touches nothing real.
"""
import json, os, shutil, sys, tempfile, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import check              # noqa: E402

import hal_tokens as ht                 # noqa: E402

MIN = 60000


# -- 1. weighing one call --------------------------------------------------------------------------
# The shape of the price list: an output token counts five, a fresh input token one, a cache read a
# tenth, and writing the cache costs more than reading it.
plain = {"input_tokens": 100, "output_tokens": 100, "cache_read_input_tokens": 0}
w, o, c = ht.weigh(plain, "claude-opus-5")
check(abs(w - (100 + 5 * 100)) < 1e-6, "output is worth five inputs (got %r)" % w)
check(o == 100 and c == 100, "the secondary totals stay raw (%r, %r)" % (o, c))

# The one that matters: a turn emitting little but re-sending a lot is NOT cheap, and output-only
# accounting would call it nothing at all.
heavy = {"input_tokens": 2, "output_tokens": 1200, "cache_read_input_tokens": 469000,
         "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 1185}}
w2, o2, c2 = ht.weigh(heavy, "claude-opus-5")
check(w2 > 5 * o2, "a cache-heavy turn weighs far more than its output suggests (%r vs %r)" % (w2, o2))
check(abs(w2 - (2 + 2.0 * 1185 + 0.10 * 469000 + 5.0 * 1200)) < 1e-6, "and by the stated formula")

# The breakdown is preferred; the flat field is the fallback, at the cheaper 5-minute rate.
flat = {"cache_creation_input_tokens": 1000, "input_tokens": 0, "output_tokens": 0}
wf, _, _ = ht.weigh(flat, "claude-opus-5")
check(abs(wf - 1250.0) < 1e-6, "a flat cache_creation counts at the 5-minute rate (got %r)" % wf)

# Thinking tokens are a SUBSET of output tokens - adding them would double-count silently.
think = {"output_tokens": 1000, "output_tokens_details": {"thinking_tokens": 900},
         "input_tokens": 0, "cache_read_input_tokens": 0}
wt, _, _ = ht.weigh(think, "claude-opus-5")
check(abs(wt - 5000.0) < 1e-6, "thinking is inside output, not beside it (got %r)" % wt)

# So are iterations: the same numbers again, one level down.
iters = dict(plain); iters["iterations"] = [dict(plain), dict(plain)]
wi, _, _ = ht.weigh(iters, "claude-opus-5")
check(abs(wi - w) < 1e-6, "iterations restate the top level and must not be added (got %r)" % wi)

for model, expect in (("claude-opus-5", 1.0), ("claude-sonnet-5", 0.6), ("claude-haiku-4-5", 0.2)):
    got = ht._model_weight(model)
    check(abs(got - expect) < 1e-9, "%s weighs %r (got %r)" % (model, expect, got))
check(ht._model_weight("<synthetic>") == 1.0, "an unknown model assumes the dear one, not the cheap one")
check(ht._model_weight(None) == 1.0, "and so does a missing one")
print("weighing: output=5x, cache read=0.1x, thinking and iterations not double-counted")


# -- 2. tailing, not re-reading ----------------------------------------------------------------------
tmp = tempfile.mkdtemp(prefix="hud-tok-")
f = os.path.join(tmp, "t.jsonl")
with open(f, "w", encoding="utf-8") as fh:
    fh.write("one\ntwo\n")
lines, off = ht._read_new(f, 0)
check(lines == ["one", "two"], "a first read takes everything (got %r)" % lines)
check(off == os.path.getsize(f), "and leaves the offset at the end")

lines, off2 = ht._read_new(f, off)
check(lines == [], "nothing new means nothing read")
check(off2 == off, "and the offset does not move")

with open(f, "a", encoding="utf-8") as fh:
    fh.write("three\n")
lines, off3 = ht._read_new(f, off2)
check(lines == ["three"], "only the new line comes back (got %r)" % lines)

# A record still being written must be left alone, or it is parsed as truncated JSON and lost.
with open(f, "a", encoding="utf-8") as fh:
    fh.write('{"partial":')
lines, off4 = ht._read_new(f, off3)
check(lines == [], "a line with no newline yet is not consumed (got %r)" % lines)
check(off4 == off3, "and the offset waits for it")
with open(f, "a", encoding="utf-8") as fh:
    fh.write('1}\n')
lines, _ = ht._read_new(f, off4)
check(lines == ['{"partial":1}'], "then arrives whole (got %r)" % lines)

# A shorter file at a path we know is a NEW file, not a rewind.
with open(f, "w", encoding="utf-8") as fh:
    fh.write("fresh\n")
lines, _ = ht._read_new(f, 10_000)
check(lines == ["fresh"], "a truncated file is re-read from the start (got %r)" % lines)
check(ht._read_new(os.path.join(tmp, "nope.jsonl"), 0) == ([], 0), "a missing file is not an error")
print("tailing: incremental, partial lines withheld, truncation detected")


# -- 3. the rate is per window, not per span ---------------------------------------------------------
now = int(time.time() * 1000)
burst = [[now - 9 * MIN, 100000.0, 1000, 5000], [now - 9 * MIN + 1000, 100000.0, 1000, 5000]]
tpm, opm, cpm = ht._rate(burst, now)
check(abs(tpm - 200000.0 / (ht.WINDOW_MS / 60000.0)) < 1e-6,
      "a burst is spread over the whole window, not over the second it took (got %r)" % tpm)
check(ht._rate([], now) == (0.0, 0.0, 0.0), "no samples, no rate")
old = [[now - 99 * MIN, 999999.0, 1, 1]]
check(ht._rate(old, now)[0] == 0.0, "and samples older than the window count for nothing")
print("rate: %s weighted/min from a burst two samples wide" % "{:,.0f}".format(tpm))


# -- 4. end to end, including the dedup that halves the answer ---------------------------------------
_saved = (ht.PROJECTS_DIR, ht.CACHE)
ht.PROJECTS_DIR = os.path.join(tmp, "projects")
ht.CACHE = os.path.join(tmp, "tokens.json")
d = os.path.join(ht.PROJECTS_DIR, "proj", "sess", "subagents", "deep")
os.makedirs(d)


def _rec(mid, out, when_ms):
    from datetime import datetime, timezone
    ts = datetime.fromtimestamp(when_ms / 1000.0, timezone.utc).isoformat().replace("+00:00", "Z")
    return json.dumps({"type": "assistant", "timestamp": ts,
                       "message": {"id": mid, "model": "claude-opus-5",
                                   "usage": {"input_tokens": 0, "output_tokens": out,
                                             "cache_read_input_tokens": 0}}})


main = os.path.join(ht.PROJECTS_DIR, "proj", "s.jsonl")
with open(main, "w", encoding="utf-8") as fh:
    # Three records, ONE call: this is what a multi-block assistant turn looks like on disk.
    for _ in range(3):
        fh.write(_rec("msg-a", 1000, now - 2 * MIN) + "\n")
    fh.write(_rec("msg-b", 500, now - 1 * MIN) + "\n")
    fh.write(json.dumps({"type": "user", "message": {"content": "hi"}}) + "\n")
sub = os.path.join(d, "agent-1.jsonl")
with open(sub, "w", encoding="utf-8") as fh:
    fh.write(_rec("msg-c", 2000, now - 1 * MIN) + "\n")

u = ht.refresh(force=True)
check(u["n"] == 3, "three distinct calls, not the five records on disk (got %d)" % u["n"])
check(len(u["hot"]) == 2, "the subagent tree is walked as well as the main transcript (got %d)"
                          % len(u["hot"]))
expect_out = (1000 + 500 + 2000) / (ht.WINDOW_MS / 60000.0)
check(abs(u["opm"] - round(expect_out)) <= 1,
      "output/min counts each call once and includes the subagent (got %r, want %r)"
      % (u["opm"], round(expect_out)))

# Running again must add nothing: the bytes have already been read and the ids already seen.
before = u["n"]
u2 = ht.refresh(force=True)
check(u2["n"] == before, "a second pass over unchanged files adds nothing (%d -> %d)" % (before, u2["n"]))

# A new call in the same file is picked up, and a repeat of an id already seen is not.
with open(main, "a", encoding="utf-8") as fh:
    fh.write(_rec("msg-a", 1000, now) + "\n")     # same id again: a later content block
    fh.write(_rec("msg-d", 300, now) + "\n")      # genuinely new
u3 = ht.refresh(force=True)
check(u3["n"] == before + 1, "only the genuinely new call is added (%d -> %d)" % (before, u3["n"]))
check(len(u3["ids"]) == 4, "and four distinct ids have been seen (got %d)" % len(u3["ids"]))

ht.PROJECTS_DIR, ht.CACHE = _saved
shutil.rmtree(tmp, ignore_errors=True)
print("end to end: 6 records on disk -> 4 calls counted, subagents included, tail resumed")

# -- 6. which chat is spending it ------------------------------------------------------------------
# A chat's own transcript is <slug>/<sessionId>.jsonl; anything its sub-agents ran lives under
# <slug>/<sessionId>/subagents/... So a sub-agent's spend bills to the chat that started it rather
# than floating free - which matters, because on a busy chat the subagent tree is the larger half.
check(ht._sid_of("/p/proj/6650e016-dee6-41ce.jsonl") == "6650e016", "a transcript names its chat")
check(ht._sid_of("/p/proj/6650e016-dee6/subagents/wf_1/agent-abc.jsonl") == "6650e016",
      "and so does anything its sub-agents ran")
check(ht._sid_of(r"C:\p\proj\6650e016-x\subagents\a\b.jsonl") == "6650e016",
      "on Windows separators too")
check(ht._sid_of("") == "?", "and an unparseable path is not silently attributed to someone")

now6 = int(time.time() * 1000)
samples = [[now6 - 60000, 100.0, 10, 50, "aaaa1111"],
           [now6 - 30000, 500.0, 30, 90, "bbbb2222"],
           [now6 - 10000, 200.0, 20, 60, "aaaa1111"],
           [now6 - 99 * MIN, 9999.0, 1, 1, "cccc3333"]]
rows = ht.by_chat(samples, now6)
check([r[0] for r in rows] == ["bbbb2222", "aaaa1111"],
      "biggest spender first, and the stale one is gone (got %r)" % rows)
mins = ht.WINDOW_MS / 60000.0
check(abs(rows[0][1] - 500 / mins) < 1, "rates are per minute over the window (got %r)" % rows[0][1])
check(abs(rows[1][1] - 300 / mins) < 1,
      "and a chat's separate calls are summed into one row (got %r)" % rows[1][1])
check(len(ht.by_chat(samples, now6, top=1)) == 1, "the list is capped for the panel")
check(ht.by_chat([[now6, 0.4, 0, 0, "x"]], now6) == [],
      "and a chat spending essentially nothing is not listed")
print("attribution: sub-agents bill to their parent chat, biggest first")


# -- 7. the running total is spend, not history ------------------------------------------------------
# A cold start tails up to four megabytes of existing transcript. Every one of those records is
# history, and counting them would inject a phantom of millions of tokens with no matching movement
# in the plan's own figure - which is exactly the pair that would ruin the calibration that reads it.
tmp7 = tempfile.mkdtemp(prefix="hud-tok7-")
_sv7 = (ht.PROJECTS_DIR, ht.CACHE)
ht.PROJECTS_DIR = os.path.join(tmp7, "projects")
ht.CACHE = os.path.join(tmp7, "tokens.json")
os.makedirs(os.path.join(ht.PROJECTS_DIR, "proj"))
old_ms = now6 - 90 * MIN
with open(os.path.join(ht.PROJECTS_DIR, "proj", "s.jsonl"), "w", encoding="utf-8") as fh:
    fh.write(_rec("old-1", 100000, old_ms) + "\n")      # an hour and a half ago
    fh.write(_rec("new-1", 100, now6 - 60000) + "\n")   # a minute ago

u7 = ht.refresh(force=True)
check(u7["n"] == 1, "the ancient record is not in the window (got %d samples)" % u7["n"])
check(abs(u7["total"] - 500.0) < 1e-6,
      "and only the recent one counts toward the total (got %r, not the 500,000 the old one weighs)"
      % u7["total"])
ht.PROJECTS_DIR, ht.CACHE = _sv7
shutil.rmtree(tmp7, ignore_errors=True)
print("total: %s from a cold start over an hour of history - spend, not archaeology" % u7["total"])


print("\nOK - tokens are weighed the way the plan charges, counted once, and read once")
