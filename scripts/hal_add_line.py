#!/usr/bin/env python3
"""Synthesize ONE new HAL line via XTTS and append it to the pool manifest.
Idempotent (skips text already in the pool) and race-tolerant (atomic manifest replace).
Invoked detached by the Stop hook when no existing pool line fit the situation."""
import sys, os, json, hashlib
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

POOL_DIR = os.path.join(os.path.expanduser("~"), ".claude", "hal_pool")
MANIFEST = os.path.join(POOL_DIR, "manifest.json")


def load():
    try:
        return json.loads(open(MANIFEST).read())
    except Exception:
        return []


def main():
    if len(sys.argv) < 2:
        return
    text = sys.argv[1].strip()
    if not text:
        return
    os.makedirs(POOL_DIR, exist_ok=True)

    # skip if this exact line already exists
    for e in load():
        if e.get("text", "").strip().lower() == text.lower():
            return

    h = hashlib.md5(text.encode("utf-8")).hexdigest()[:10]
    fname = f"hal_auto_{h}.mp3"
    fpath = os.path.join(POOL_DIR, fname)

    if not (os.path.exists(fpath) and os.path.getsize(fpath) > 0):
        import hal_tts
        hal_tts.synthesize_hal(text, fpath)   # ~40s on CPU; runs detached
    if not (os.path.exists(fpath) and os.path.getsize(fpath) > 0):
        return

    # re-read right before writing to minimise races, then atomic replace
    cur = load()
    if any(e.get("file") == fname or e.get("text", "").strip().lower() == text.lower()
           for e in cur):
        return
    cur.append({"file": fname, "text": text})
    tmp = MANIFEST + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cur, f, indent=2)
    os.replace(tmp, MANIFEST)


if __name__ == "__main__":
    main()
