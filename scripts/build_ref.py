#!/usr/bin/env python3
"""Find the longest CONTINUOUS HAL monologue in the raw compilation (via Silero-VAD)
and make a clean single-passage XTTS reference from it (XTTS conditions best on one
continuous calm passage, not a silence-stripped stitch of many different readings)."""
import os, subprocess, tempfile
import numpy as np, soundfile as sf
from silero_vad import load_silero_vad, get_speech_timestamps, read_audio

RAW = os.path.join(os.path.expanduser("~"), ".claude", "hal_src", "hal_raw.wav")
OUT = os.path.join(os.path.expanduser("~"), ".claude", "hal_voice_ref_clean2.wav")
RNNOISE_DIR = os.path.join(os.path.expanduser("~"), ".claude", "rnnoise")
FFMPEG = r"C:\Users\braxt\AppData\Local\Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-8.1.1-full_build\bin\ffmpeg.exe"
SR16 = 16000
TARGET = 11.0   # seconds of continuous speech to capture

print("loading VAD + scanning 442s...", flush=True)
model = load_silero_vad()
wav = read_audio(RAW, sampling_rate=SR16)
ts = get_speech_timestamps(wav, model, sampling_rate=SR16, threshold=0.5,
                           min_speech_duration_ms=200, min_silence_duration_ms=300)
if not ts:
    raise SystemExit("no speech found")

# merge segments whose gap < 0.5s into continuous passages
passages = []
cur = [ts[0]["start"], ts[0]["end"]]
for t in ts[1:]:
    if t["start"] - cur[1] < 0.5 * SR16:
        cur[1] = t["end"]
    else:
        passages.append(tuple(cur)); cur = [t["start"], t["end"]]
passages.append(tuple(cur))
passages.sort(key=lambda p: p[1] - p[0], reverse=True)

best = passages[0]
print(f"longest continuous passage: {(best[1]-best[0])/SR16:.1f}s at t={best[0]/SR16:.1f}s", flush=True)
start16 = best[0]
end16 = min(best[1], best[0] + int(TARGET * SR16))

data, sr = sf.read(RAW)
if getattr(data, "ndim", 1) > 1:
    data = data.mean(axis=1)
data = np.asarray(data, dtype=np.float32)
s = int(start16 / SR16 * sr)
e = int(end16 / SR16 * sr)
seg = data[s:e]

tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False); tmp.close()
sf.write(tmp.name, seg, sr)
# light RNNoise + sub-rumble clear, to 24k mono
subprocess.run([FFMPEG, "-y", "-i", os.path.abspath(tmp.name),
                "-af", "arnndn=m=sh.rnnn:mix=0.9,highpass=f=70",
                "-ar", "24000", "-ac", "1", os.path.abspath(OUT)],
               cwd=RNNOISE_DIR, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
os.remove(tmp.name)
dur = len(seg) / sr
print(f"wrote {OUT}  ({dur:.1f}s, {os.path.getsize(OUT)} bytes)", flush=True)
