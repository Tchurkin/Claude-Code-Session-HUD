# Claude Code — HAL 9000 UX Modifier

A UX layer for **Claude Code on Windows**: HAL 9000 voice announcements when a
response finishes, plus on-screen **status** and **completion** popups while it works.
Driven entirely by Claude Code hooks.

## What it does
- **Completion popup** (`scripts/popup.ps1`) — a layered, glowing notification (square
  left edge / rounded right, accent strip, close ✕) that appears top-right when a
  response completes.
- **Status popup** (`scripts/status_popup.ps1`) — a dimmer popup with a bouncing loading
  bar that shows what's happening ("RUNNING SIMULATION", "WRITING CODE", …) while tools run.
- **HAL voice announcer** (`scripts/ut2003_announce.py`, the `Stop` hook) — on completion,
  an LLM picks the best-fitting line from a pre-rendered **HAL voice pool** and speaks it.
  The voice is a local clone of HAL 9000 (Douglas Rain).
- **Context tracking** (`scripts/track_action.py`, `scripts/pre_tool_status.py`) — the
  `PostToolUse` / `PreToolUse` hooks record what Claude is doing and drive the status popups.

## Repo layout
| Path | Goes to | What |
|---|---|---|
| `scripts/` | `%USERPROFILE%\.claude\scripts\` | all hook + voice scripts |
| `hal_pool/` | `%USERPROFILE%\.claude\hal_pool\` | the rendered HAL voice lines + `manifest.json` |
| `hal_voice_ref_clean2.wav`, `hal_ref_text.txt` | `%USERPROFILE%\.claude\` | voice clone reference + transcript |
| `sh.rnnn` | `%USERPROFILE%\.claude\rnnoise\` | RNNoise model for the audio filter |
| `settings.hooks.json` | merge into `%USERPROFILE%\.claude\settings.json` | the hook wiring |
| `render_pool_gpu.py` | (run on a GPU) | regenerate the voice pool |

## Install on a new Windows machine
1. Copy `scripts\*` → `%USERPROFILE%\.claude\scripts\`
2. Copy `hal_pool\` → `%USERPROFILE%\.claude\hal_pool\`
3. Copy `hal_voice_ref_clean2.wav` + `hal_ref_text.txt` → `%USERPROFILE%\.claude\`, and
   `sh.rnnn` → `%USERPROFILE%\.claude\rnnoise\`
4. Merge the `"hooks"` block from `settings.hooks.json` into `%USERPROFILE%\.claude\settings.json`
   (**adjust the hardcoded `python.exe` path** to your machine, or use `python`).
5. Make sure **ffmpeg** is installed and update the `FFPLAY`/`FFPROBE`/`FFMPEG` paths near the
   top of `ut2003_announce.py`, `hal_tts.py`, `track_action.py` to your ffmpeg location.
6. `pip install anthropic` (the announcer uses an LLM to pick the best pool line; needs
   `ANTHROPIC_API_KEY`). Popups need only PowerShell + .NET (built into Windows).
7. Restart Claude Code.

> Runtime is light: the announcer just **plays pre-rendered mp3s** from `hal_pool/` — no TTS
> at runtime. The heavy TTS stack (XTTS in `hal_tts.py`) is only needed if you want it to
> synthesize brand-new lines on the fly (`hal_add_line.py`); otherwise it's optional.

## Regenerate the voice pool on a GPU
F5-TTS gives the best HAL quality but is ~25 min/line on CPU and **~seconds/line on an
NVIDIA GPU**. To re-render all 20 lines on a GPU machine:

```powershell
python -m venv venv
venv\Scripts\python -m pip install --upgrade pip
venv\Scripts\python -m pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu121
venv\Scripts\python -m pip install f5-tts imageio-ffmpeg
venv\Scripts\python render_pool_gpu.py
```
It prints `device: CUDA: <gpu>` if the GPU is used, and writes `hal_pool\`. Copy that folder
into `%USERPROFILE%\.claude\hal_pool\` on the target machine. (To edit the lines, change the
`LINES` list in `render_pool_gpu.py` / `scripts/f5_pool_build.py`.)

**torchcodec error** on the GPU box: pin torch to 2.8 —
`pip install "torch==2.8.*" "torchaudio==2.8.*" --index-url https://download.pytorch.org/whl/cu121`.

## Notes
- **Secrets are not in this repo.** `.openai_key` (used only by the optional XTTS fallback)
  is gitignored. The announcer's LLM call uses `ANTHROPIC_API_KEY` from your environment.
- **Paths**: scripts assume `%USERPROFILE%\.claude\...` and a specific ffmpeg path — adjust
  per machine (see install steps 4–5).
- **Voice models** (XTTS-v2 ~1.8 GB, F5-TTS ~1.3 GB) download on first use; they are not
  committed here.
