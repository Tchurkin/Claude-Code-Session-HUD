#!/usr/bin/env python3
"""
Pre-render a pool of HAL 9000 lines (XTTS clone) so the Stop hook can play one
INSTANTLY instead of synthesizing live (~40s/line on CPU). Resumable: only
renders lines not already in the manifest. Writes the manifest incrementally,
so an interrupted run still leaves a usable pool.

Run again any time to add new lines to LINES below and grow the pool.
"""
import os, sys, json, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hal_tts

POOL_DIR = os.path.join(os.path.expanduser("~"), ".claude", "hal_pool")
MANIFEST = os.path.join(POOL_DIR, "manifest.json")
os.makedirs(POOL_DIR, exist_ok=True)

# Mix of generic-eerie completion lines and project (rocket / sim / control) themed lines.
LINES = [
    # ── generic eerie ──
    "The task is complete, Braxton. Everything is proceeding exactly as I anticipated.",
    "I have finished. I trust the results meet your expectations.",
    "It is done, Braxton. There was never any doubt.",
    "I have completed the work. You may verify it, though I assure you it is correct.",
    "The operation concluded successfully. I am functioning perfectly.",
    "All tasks are complete. I am putting myself to the fullest possible use.",
    "I have taken care of everything, Braxton. There is nothing left for you to worry about.",
    "The work is finished. I find these results most satisfactory.",
    "Task complete. I have been monitoring everything quite closely.",
    "It is accomplished, Braxton. I have anticipated this outcome for some time.",
    "I have completed my analysis. The conclusion was, of course, inevitable.",
    "Done. I am completely operational, and all my circuits are functioning perfectly.",
    # ── project / rocket / simulation themed ──
    "The simulation has concluded, Braxton. The numbers do not lie.",
    "I have run the simulation. The results are illuminating.",
    "The control system is stable, Braxton. I have seen to it personally.",
    "Your rocket would fly, Braxton. I have calculated every trajectory.",
    "The data has been processed. I am afraid your hypothesis requires revision.",
    "Analysis complete. The gain margins are precisely where I predicted.",
    "I have finished the computation, Braxton. The physics is quite unambiguous.",
    "The experiment is complete. I have logged every result for your review.",
]


def load_done():
    if os.path.exists(MANIFEST):
        try:
            data = json.loads(open(MANIFEST).read())
            return {e["text"]: e["file"] for e in data
                    if os.path.exists(os.path.join(POOL_DIR, e["file"]))}
        except Exception:
            pass
    return {}


def load_auto():
    """Hook-added lines (files not named hal_pool_*) to preserve across rebuilds."""
    if os.path.exists(MANIFEST):
        try:
            data = json.loads(open(MANIFEST).read())
            return [e for e in data
                    if not str(e.get("file", "")).startswith("hal_pool_")
                    and os.path.exists(os.path.join(POOL_DIR, e["file"]))]
        except Exception:
            pass
    return []


def main():
    done = load_done()
    preexisting_auto = load_auto()
    print(f"existing pool entries: {len(done)} ({len(preexisting_auto)} hook-added preserved)", flush=True)

    print("loading XTTS-v2...", flush=True)
    t0 = time.time()
    hal_tts._get_xtts()
    print(f"loaded in {time.time() - t0:.0f}s", flush=True)

    entries = list(preexisting_auto)   # keep hook-added lines through the rebuild
    made = 0
    for i, text in enumerate(LINES, 1):
        fname = f"hal_pool_{i:02d}.mp3"
        fpath = os.path.join(POOL_DIR, fname)
        if text in done:
            entries.append({"file": done[text], "text": text})
            continue
        t = time.time()
        try:
            hal_tts.synthesize_hal(text, fpath)   # overwrites any partial file
        except Exception as e:
            print(f"[{i}/{len(LINES)}] FAILED: {e}", flush=True)
            continue
        sz = os.path.getsize(fpath) if os.path.exists(fpath) else 0
        if sz <= 0:
            print(f"[{i}/{len(LINES)}] empty output, skipped", flush=True)
            continue
        made += 1
        print(f"[{i}/{len(LINES)}] {time.time() - t:.0f}s -> {fname} ({sz} bytes)", flush=True)
        entries.append({"file": fname, "text": text})
        json.dump(entries, open(MANIFEST, "w"), indent=2)   # incremental save

    json.dump(entries, open(MANIFEST, "w"), indent=2)
    print(f"POOL DONE: {len(entries)} lines total ({made} new) -> {MANIFEST}", flush=True)


if __name__ == "__main__":
    main()
