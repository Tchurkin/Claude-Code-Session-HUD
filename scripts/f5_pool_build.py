#!/usr/bin/env python3
"""Runs INSIDE tts_venv. Builds the HAL pool with F5-TTS (higher quality than XTTS,
but ~20-30 min/line on CPU -> meant to run as a long/overnight background job).
Resumable: skips lines already in the manifest; preserves hook-added (hal_auto_) lines.
The Stop hook plays these mp3s; live novel lines still use the fast XTTS fallback."""
import os, sys, json, time, subprocess, tempfile

import torch
torch.set_num_threads(os.cpu_count() or 14)   # use all cores

HOME = os.path.expanduser("~")
REF      = os.path.join(HOME, ".claude", "hal_voice_ref_clean2.wav")
REF_TXT  = os.path.join(HOME, ".claude", "hal_ref_text.txt")
POOL_DIR = os.path.join(HOME, ".claude", "hal_pool")
MANIFEST = os.path.join(POOL_DIR, "manifest.json")
RNNOISE_DIR = os.path.join(HOME, ".claude", "rnnoise")
FFMPEG = r"C:\Users\braxt\AppData\Local\Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-8.1.1-full_build\bin\ffmpeg.exe"
os.makedirs(POOL_DIR, exist_ok=True)

# Identical HAL post-filter to the XTTS path (so pool + live fallback sound consistent).
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

LINES = [
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


def load_manifest():
    try:
        return json.loads(open(MANIFEST).read())
    except Exception:
        return []


def main():
    full = load_manifest()
    done = {e["text"]: e["file"] for e in full
            if os.path.exists(os.path.join(POOL_DIR, e["file"]))}
    auto = [e for e in full
            if not str(e.get("file", "")).startswith("hal_pool_")
            and os.path.exists(os.path.join(POOL_DIR, e["file"]))]
    print(f"existing: {len(done)} ({len(auto)} hook-added preserved)", flush=True)

    ref_text = open(REF_TXT, encoding="utf-8").read().strip()
    from f5_tts.api import F5TTS
    print("loading F5-TTS...", flush=True)
    t0 = time.time()
    f5 = F5TTS()
    print(f"loaded in {time.time()-t0:.0f}s; threads={torch.get_num_threads()}", flush=True)

    entries = list(auto)
    for i, text in enumerate(LINES, 1):
        fname = f"hal_pool_{i:02d}.mp3"
        if text in done:
            entries.append({"file": done[text], "text": text})
            continue
        t = time.time()
        raw = tempfile.NamedTemporaryFile(suffix=".wav", delete=False); raw.close()
        try:
            f5.infer(ref_file=REF, ref_text=ref_text, gen_text=text,
                     file_wave=raw.name, remove_silence=True, nfe_step=32)
            subprocess.run([FFMPEG, "-y", "-i", os.path.abspath(raw.name),
                            "-af", CLONE_FILTER, "-q:a", "3",
                            os.path.abspath(os.path.join(POOL_DIR, fname))],
                           cwd=RNNOISE_DIR, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        finally:
            try: os.remove(raw.name)
            except Exception: pass
        sz = os.path.getsize(os.path.join(POOL_DIR, fname)) if os.path.exists(os.path.join(POOL_DIR, fname)) else 0
        if sz <= 0:
            print(f"[{i}/{len(LINES)}] FAILED (empty)", flush=True)
            continue
        print(f"[{i}/{len(LINES)}] {time.time()-t:.0f}s -> {fname} ({sz} bytes)", flush=True)
        entries.append({"file": fname, "text": text})
        json.dump(entries, open(MANIFEST, "w"), indent=2)   # incremental + resumable

    json.dump(entries, open(MANIFEST, "w"), indent=2)
    print(f"F5 POOL DONE: {len(entries)} lines -> {MANIFEST}", flush=True)


if __name__ == "__main__":
    main()
