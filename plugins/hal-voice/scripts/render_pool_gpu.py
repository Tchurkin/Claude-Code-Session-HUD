#!/usr/bin/env python3
"""
Bulk-render the base HAL voice pool with F5-TTS (uses CUDA automatically if present)
at full quality (11s reference / nfe_step=32) and the HAL post-filter.

Writes mp3s + manifest.json (with dur_ms) into ../hal_pool inside the plugin, reading
the voice reference from ../reference.  Resumable: skips lines already rendered and
PRESERVES hook-synthesized (hal_auto_*) lines across rebuilds.

Run it in the f5-tts venv:   venv\\Scripts\\python plugins\\hal-voice\\scripts\\render_pool_gpu.py
Set HAL_NAME to re-target the spoken name (default "Braxton").
"""
import os, re, json, time, subprocess, tempfile

SCRIPTS  = os.path.dirname(os.path.abspath(__file__))
PLUGIN   = os.path.dirname(SCRIPTS)
REF_DIR  = os.path.join(PLUGIN, "reference")
REF      = os.path.join(REF_DIR, "hal_voice_ref_clean2.wav")
REF_TXT  = os.path.join(REF_DIR, "hal_ref_text.txt")
POOL_DIR = os.path.join(PLUGIN, "hal_pool")
MANIFEST = os.path.join(POOL_DIR, "manifest.json")
NAME     = os.environ.get("HAL_NAME", "Braxton")
os.makedirs(POOL_DIR, exist_ok=True)

import imageio_ffmpeg
FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()

# sh.rnnn lives in REF_DIR, referenced by bare name with cwd=REF_DIR (avoids ':' escaping).
CLONE_FILTER = (
    "arnndn=m=sh.rnnn:mix=0.85,"
    "highpass=f=85,"
    "equalizer=f=200:width_type=o:width=2:g=1.5,"
    "equalizer=f=3000:width_type=o:width=1.8:g=2,"
    "lowpass=f=9500,"
    "acompressor=threshold=-18dB:ratio=2.5:attack=15:release=250:makeup=3,"
    "aecho=0.85:0.5:30:0.13,"
    "alimiter=limit=0.95"
)

_LINES = [
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
    "The simulation has concluded, Braxton. The numbers do not lie.",
    "I have run the simulation. The results are illuminating.",
    "The control system is stable, Braxton. I have seen to it personally.",
    "Your rocket would fly, Braxton. I have calculated every trajectory.",
    "The data has been processed. I am afraid your hypothesis requires revision.",
    "Analysis complete. The gain margins are precisely where I predicted.",
    "I have finished the computation, Braxton. The physics is quite unambiguous.",
    "The experiment is complete. I have logged every result for your review.",
]
LINES = [ln.replace("Braxton", NAME) for ln in _LINES]

# Failure ('fail') and awaiting-input ('wait') sets live in hal_lines so the runtime can
# also synthesize them on demand; baking them here makes them instant + shared via git.
import hal_lines
FAIL_LINES = hal_lines.for_name(hal_lines.FAIL_LINES, NAME)
WAIT_LINES = hal_lines.for_name(hal_lines.WAIT_LINES, NAME)


def duration_ms(path):
    try:
        r = subprocess.run([FFMPEG, "-i", path], capture_output=True, text=True)
        m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)", r.stderr)
        if m:
            h, mi, s = m.groups()
            return int((int(h) * 3600 + int(mi) * 60 + float(s)) * 1000)
    except Exception:
        pass
    return 6000


def load_manifest():
    try:
        return json.loads(open(MANIFEST, encoding="utf-8").read())
    except Exception:
        return []


def render_group(f5, ref_text, lines, fname_fmt, kind, entries, prev):
    """Render one group of lines into the pool, appending to `entries` and writing the
    manifest after each line (resumable). Skips a line whose mp3 already exists with the
    same text (so a re-run is cheap, but changing HAL_NAME re-renders the changed lines)."""
    for i, text in enumerate(lines, 1):
        fname = fname_fmt.format(i=i)
        out   = os.path.join(POOL_DIR, fname)
        have  = os.path.exists(out) and os.path.getsize(out) > 0
        if have and prev.get(fname, {}).get("text") == text:
            dur = prev[fname].get("dur_ms") or duration_ms(out)
            print(f"  skip {fname} (exists, {dur} ms)", flush=True)
        else:
            raw = tempfile.NamedTemporaryFile(suffix=".wav", delete=False); raw.close()
            t = time.time()
            f5.infer(ref_file=REF, ref_text=ref_text, gen_text=text,
                     file_wave=raw.name, remove_silence=True, nfe_step=32)
            subprocess.run([FFMPEG, "-y", "-i", raw.name, "-af", CLONE_FILTER, "-q:a", "3", out],
                           cwd=REF_DIR, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try: os.remove(raw.name)
            except Exception: pass
            dur = duration_ms(out)
            print(f"  {fname} {time.time()-t:.1f}s ({dur} ms)", flush=True)
        e = {"file": fname, "text": text, "dur_ms": dur}
        if kind:
            e["kind"] = kind
        entries.append(e)
        json.dump(entries, open(MANIFEST, "w"), indent=2)


def main():
    full = load_manifest()
    prev = {e.get("file", ""): e for e in full}
    # Preserve hook-synthesized (hal_auto_*) lines across rebuilds, with their kind tags.
    autos = [e for e in full
             if not str(e.get("file", "")).startswith("hal_pool_")
             and os.path.exists(os.path.join(POOL_DIR, e["file"]))]
    print(f"existing autos preserved: {len(autos)}", flush=True)

    ref_text = open(REF_TXT, encoding="utf-8").read().strip()

    import torch
    dev = ("CUDA: " + torch.cuda.get_device_name(0)) if torch.cuda.is_available() else "CPU (no GPU - will be slow!)"
    print("device:", dev, flush=True)

    from f5_tts.api import F5TTS
    print("loading F5-TTS...", flush=True)
    f5 = F5TTS()

    entries = list(autos)
    print("completion lines:", flush=True)
    render_group(f5, ref_text, LINES,      "hal_pool_{i:02d}.mp3",      None,   entries, prev)
    print("failure lines:", flush=True)
    render_group(f5, ref_text, FAIL_LINES, "hal_pool_fail_{i:02d}.mp3", "fail", entries, prev)
    print("awaiting-input lines:", flush=True)
    render_group(f5, ref_text, WAIT_LINES, "hal_pool_wait_{i:02d}.mp3", "wait", entries, prev)

    json.dump(entries, open(MANIFEST, "w"), indent=2)
    print(f"\nDONE: {len(entries)} lines -> {POOL_DIR}", flush=True)
    print("Sync to your other devices with:  python plugins/hal-voice/scripts/hal_pool_sync.py", flush=True)


if __name__ == "__main__":
    main()
