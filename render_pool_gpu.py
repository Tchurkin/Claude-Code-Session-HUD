#!/usr/bin/env python3
"""
Self-contained HAL pool renderer for a GPU machine.
Renders all 20 HAL lines with F5-TTS (uses CUDA automatically if present) at full
quality (11s reference / nfe_step=32), applies the same HAL post-filter, and writes
final mp3s + manifest.json into ./hal_pool/.  Then copy ./hal_pool/ back to the
laptop's  C:\\Users\\<you>\\.claude\\hal_pool\\  and the announcer uses it as-is.

Resumable: re-running skips lines already rendered.
All inputs (reference wav, transcript, sh.rnnn) live next to this script.
"""
import os, json, time, subprocess, tempfile

HERE     = os.path.dirname(os.path.abspath(__file__))
REF      = os.path.join(HERE, "hal_voice_ref_clean2.wav")
REF_TXT  = os.path.join(HERE, "hal_ref_text.txt")
POOL_DIR = os.path.join(HERE, "hal_pool")
MANIFEST = os.path.join(POOL_DIR, "manifest.json")
os.makedirs(POOL_DIR, exist_ok=True)

import imageio_ffmpeg
FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()

# Same HAL post-filter as the laptop pipeline (sh.rnnn is in HERE, referenced by bare
# name with cwd=HERE so there's no Windows drive-colon escaping in the filtergraph).
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


def main():
    ref_text = open(REF_TXT, encoding="utf-8").read().strip()

    import torch
    dev = "CUDA: " + torch.cuda.get_device_name(0) if torch.cuda.is_available() else "CPU (no GPU - will be slow!)"
    print("device:", dev, flush=True)

    from f5_tts.api import F5TTS
    print("loading F5-TTS...", flush=True)
    f5 = F5TTS()

    done = {}
    if os.path.exists(MANIFEST):
        try:
            done = {e["text"]: e["file"] for e in json.load(open(MANIFEST))
                    if os.path.exists(os.path.join(POOL_DIR, e["file"]))}
        except Exception:
            pass

    entries = []
    for i, text in enumerate(LINES, 1):
        fname = f"hal_pool_{i:02d}.mp3"
        if text in done:
            entries.append({"file": done[text], "text": text})
            continue
        raw = tempfile.NamedTemporaryFile(suffix=".wav", delete=False); raw.close()
        t = time.time()
        f5.infer(ref_file=REF, ref_text=ref_text, gen_text=text,
                 file_wave=raw.name, remove_silence=True, nfe_step=32)
        subprocess.run([FFMPEG, "-y", "-i", raw.name, "-af", CLONE_FILTER,
                        "-q:a", "3", os.path.join(POOL_DIR, fname)],
                       cwd=HERE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try: os.remove(raw.name)
        except Exception: pass
        print(f"[{i}/{len(LINES)}] {time.time()-t:.1f}s -> {fname}", flush=True)
        entries.append({"file": fname, "text": text})
        json.dump(entries, open(MANIFEST, "w"), indent=2)

    json.dump(entries, open(MANIFEST, "w"), indent=2)
    print(f"\nDONE: {len(entries)} lines -> {POOL_DIR}", flush=True)
    print("Copy the hal_pool folder to your laptop's  %USERPROFILE%\\.claude\\hal_pool", flush=True)


if __name__ == "__main__":
    main()
