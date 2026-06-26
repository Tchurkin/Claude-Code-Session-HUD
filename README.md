# HAL 9000 voice — a Claude Code plugin

A UX layer for **Claude Code on Windows**: HAL 9000 speaks when a response finishes,
plus on-screen status/completion popups while Claude works. It is a real Claude Code
**plugin** (`plugins/hal-voice`), distributed from this repo's marketplace.

Two ways HAL gets a line to say, decided at runtime:

1. **Live synthesis** — on a machine with an NVIDIA GPU and the F5-TTS venv, a warm
   daemon synthesizes a *tailored* line in HAL's cloned voice (Douglas Rain) in a few
   seconds. The announcer waits a short budget for it.
2. **Pre-rendered pool** — if synthesis would take too long (no GPU, or the daemon is
   still warming up), HAL immediately plays the best-fitting line from a pre-rendered
   **pool**. Any line synthesized live is appended to that pool, and the pool is
   **shared across your devices over git**, so every device can pick from it.

> The voice is a local clone — no audio leaves your machine. The only network call is an
> optional Claude API request that picks which pool line best fits what Claude just did
> (it degrades to a random fit if `anthropic` / `ANTHROPIC_API_KEY` isn't present).

## How it works

| Hook | Script | What |
|---|---|---|
| `Stop` | `hal_announce.py` | Pick the best pool line (LLM, optional); if a tailored line fits better and this machine can synth live, wait up to `synth_budget_ms` for the daemon, else play the pool line now and let the daemon finish the new one into the pool. Then show the completion popup + speak. |
| `PreToolUse` (Bash) | `pre_tool_status.py` | Loading status popup before long commands ("RUNNING SIMULATION", …). |
| `PostToolUse` (Bash/Write/Edit/NotebookEdit) | `track_action.py` | Record what Claude is doing (drives the Stop-hook context) + status popup. |

Supporting pieces (all under `plugins/hal-voice/scripts/`):

- `hal_tts_f5.py` — F5-TTS synthesis **and** a warm daemon (loads the ~1.3 GB model once,
  serves single-line requests over `127.0.0.1`, self-exits when idle). The announcer talks
  to it with a tiny socket client, so the announcer itself needs no ML stack.
- `hal_common.py` — portable paths, config, the pool/manifest, durations, and
  dependency-free audio playback (`play_audio.ps1`, WPF MediaPlayer — no ffplay needed).
- `hal_setup.py` — write this machine's config (detect GPU + venv).
- `hal_add_line.py` — synthesize one line into the pool (`/hal-say`).
- `hal_pool_sync.py` — git pull/merge/push the pool across devices (`/hal-sync`).
- `render_pool_gpu.py` — bulk-(re)render the base pool on a GPU.

Machine-local state lives in `~/.claude/hal_voice/` (config + scratch). The pool, the voice
reference, and the venv live in your **git clone** of this repo and are located via config —
because an installed plugin is a read-only cached copy, not a git working tree.

## Install

### 1. Clone this repo (it is both the plugin source and the shared pool home)
```powershell
git clone https://github.com/Tchurkin/hal-voice-bundle
cd hal-voice-bundle
```

### 2. (GPU machines only) create the F5-TTS venv
Required for **live synthesis**; skip on consumer machines (they use the pool).
```powershell
python -m venv venv
venv\Scripts\python -m pip install --upgrade pip
# torch 2.8 avoids the torchcodec audio-load failure on Windows; it is published for
# cu126 (not cu121). cu126 matches recent NVIDIA drivers (CUDA 12.x).
venv\Scripts\python -m pip install "torch==2.8.*" "torchaudio==2.8.*" --index-url https://download.pytorch.org/whl/cu126
venv\Scripts\python -m pip install f5-tts imageio-ffmpeg
```
(The base 20-line pool is already committed in `plugins/hal-voice/hal_pool/`. To
re-render it — e.g. to change the lines or the spoken name — run
`venv\Scripts\python plugins\hal-voice\scripts\render_pool_gpu.py`; set `HAL_NAME` to
re-target the name.)

### 3. Install the plugin
```
/plugin marketplace add C:\path\to\hal-voice-bundle
/plugin install hal-voice@hal-bundle
```
(For quick dev iteration you can instead launch with `claude --plugin-dir C:\path\to\hal-voice-bundle\plugins\hal-voice`.)

### 4. Configure this machine
```powershell
# GPU machine (live synth):
venv\Scripts\python plugins\hal-voice\scripts\hal_setup.py --name Braxton
# consumer machine (pool only):
python plugins\hal-voice\scripts\hal_setup.py --name Braxton
```
Then reload so the hooks pick everything up: **`/reload-plugins`** (or restart Claude Code).

### 5. Optional, for smarter line selection
`pip install anthropic` and set `ANTHROPIC_API_KEY` for the python that runs the hooks.
Without it, HAL still speaks — it just picks a fitting pool line at random.

## Commands
- `/hal-say <line>` — speak a line now (synthesizes + adds to the pool if new).
- `/hal-sync` — push/pull the pool across your devices over git.
- `/hal-setup` — (re)configure this machine.

## Cross-device pool sharing
New lines land in `plugins/hal-voice/hal_pool/` inside your clone. `/hal-sync`
(`hal_pool_sync.py`) commits them, `git pull`s other devices' lines, resolves the
inevitable `manifest.json` overlap by **union** (mp3 filenames are content-hashed, so they
never collide), and pushes. Other devices run sync (or just `git pull`) to receive them.
Consumers without push access still pull everyone else's lines.

## Config reference (`~/.claude/hal_voice/config.json`)
| key | meaning |
|---|---|
| `user_name` | name HAL may use in tailored lines |
| `pool_dir` / `reference_dir` | writable pool / voice reference (your clone; falls back to the bundled copies) |
| `tts_python` | venv python with f5-tts (enables live synth) |
| `pool_repo` | git repo root for `/hal-sync` |
| `gpu` | set by `hal_setup`; live synth requires it (CPU F5 is far too slow) |
| `live_synth` | master on/off for live synthesis |
| `synth_budget_ms` | how long the announcer waits for a live line before using the pool (default 6000) |
| `daemon_port` / `daemon_idle_s` | warm-daemon socket port / idle self-exit |

## Notes
- **Windows-only** today (PowerShell popups + audio). The synthesis/pool/sync logic is
  cross-platform; only the popups and player are Windows-specific.
- **No ffplay/ffprobe needed** at runtime — playback uses WPF MediaPlayer and clip
  durations are stored in the manifest.
- **Secrets** are never committed (`.gitignore` covers `*.key` / `.openai_key`).
