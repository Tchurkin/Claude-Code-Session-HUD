# HAL voice pool — GPU render (do this on the gaming PC)

This renders the 20 HAL 9000 announcement lines with **F5-TTS** at full quality
(11 s reference / 32 steps). On a CPU laptop this takes ~8 hours; on a GPU it takes
**~1–3 minutes total**. You then copy the result back to the laptop.

## What's in this folder
- `render_pool_gpu.py` — the renderer (self-contained; bundles its own ffmpeg)
- `hal_voice_ref_clean2.wav` — the HAL voice reference clip
- `hal_ref_text.txt` — transcript of that clip (F5 needs it)
- `sh.rnnn` — RNNoise model for the audio post-filter

## Prerequisites on the PC
- An **NVIDIA GPU** + recent driver
- **Python 3.10–3.12**
- (Optional but easiest) **Claude Code / VSCode** — you can just point it at this
  folder and say "follow README.md" and it'll run these steps for you.

## Steps (PowerShell, in this folder)

```powershell
# 1. make an isolated environment
python -m venv venv

# 2. install CUDA PyTorch (cu121 works for most modern GPUs; see note) + F5 + ffmpeg
venv\Scripts\python -m pip install --upgrade pip
venv\Scripts\python -m pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu121
venv\Scripts\python -m pip install f5-tts imageio-ffmpeg

# 3. render (first run downloads the ~1.3 GB F5 model, then ~1-3 min on GPU)
venv\Scripts\python render_pool_gpu.py
```

The script prints `device: CUDA: <your GPU>` if the GPU is being used. If it prints
`CPU (no GPU ...)`, the CUDA torch install didn't take — fix that before rendering.

When it finishes you'll have a **`hal_pool\`** folder here with `hal_pool_01.mp3 …
hal_pool_20.mp3` + `manifest.json`.

## Bring it back to the laptop

**Option A — git (easiest).** From this folder on the PC (hal_pool is gitignored, so
force-add it):

```powershell
git add -f hal_pool
git commit -m "rendered pool"
git push
```

Then on the laptop, Claude can `git pull` this repo and drop `hal_pool\` into
`%USERPROFILE%\.claude\hal_pool\` for you — just say "the pool is on github, install it."

**Option B — manual.** Copy the whole `hal_pool\` folder onto USB/cloud and into the
laptop at `%USERPROFILE%\.claude\hal_pool\` (overwriting what's there).

Either way, that's it — the announcer picks the new pool up automatically; no other
changes needed on the laptop.

## Notes / gotchas
- **torchcodec error** ("Could not load libtorchcodec…"): newer torch (≥2.9) routes
  audio loading through torchcodec, which often fails on Windows. Fix: install a
  slightly older torch, e.g.
  `venv\Scripts\python -m pip install "torch==2.8.*" "torchaudio==2.8.*" --index-url https://download.pytorch.org/whl/cu121`
  (this is the exact issue we hit on the laptop; 2.8 uses the simpler soundfile path).
- **CUDA version:** `cu121` is a safe default. If your GPU/driver is very new and you
  hit an install error, try `cu124` instead of `cu121` in the torch install URL.
- **Resumable:** re-running `render_pool_gpu.py` skips lines already done, so an
  interruption is harmless.
- To change the lines, edit the `LINES` list at the top of `render_pool_gpu.py`.
