#!/usr/bin/env python3
"""
One-time per-machine setup: detect capability and write ~/.claude/hal_voice/config.json.

Run it from your git clone of this repo (so it can locate the pool / reference / venv):

    # capable machine (after creating ./venv with f5-tts - see render_pool_gpu.py):
    venv\\Scripts\\python plugins\\hal-voice\\scripts\\hal_setup.py --name Braxton

    # consumer machine (no GPU): plain python is fine; live synth stays off
    python plugins\\hal-voice\\scripts\\hal_setup.py --name Braxton

Options:
    --name NAME         name HAL may use in tailored lines (default: keep/Braxton)
    --tts-python PATH   override the f5-tts venv python (default: auto-detect ./venv)
    --no-live           force pool-only even on a capable machine
"""
import os, sys, subprocess

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hal_common as hc

SCRIPTS  = os.path.dirname(os.path.abspath(__file__))
PLUGIN   = os.path.dirname(SCRIPTS)                 # .../plugins/hal-voice
REPO     = os.path.dirname(os.path.dirname(PLUGIN)) # repo root


def detect_tts_python(override):
    if override:
        return override if os.path.exists(override) else None
    for cand in (os.path.join(REPO, "venv", "Scripts", "python.exe"),
                 os.path.join(REPO, "venv", "bin", "python")):
        if os.path.exists(cand):
            return cand
    return None


def has_cuda(py):
    if not py:
        return False
    try:
        r = subprocess.run([py, "-c", "import torch,sys;sys.stdout.write('1' if torch.cuda.is_available() else '0')"],
                           capture_output=True, text=True, timeout=120)
        return r.stdout.strip() == "1"
    except Exception:
        return False


def parse_arg(flag, default=None):
    if flag in sys.argv:
        i = sys.argv.index(flag)
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return default


def main():
    cfg = hc.load_config()
    name = parse_arg("--name", cfg.get("user_name") or "Braxton")
    tts_python = detect_tts_python(parse_arg("--tts-python"))
    gpu = has_cuda(tts_python)
    live = (not "--no-live" in sys.argv) and bool(tts_python) and gpu

    pool = os.path.join(PLUGIN, "hal_pool")
    ref  = os.path.join(PLUGIN, "reference")

    cfg.update({
        "user_name":     name,
        "pool_dir":      pool if os.path.isdir(pool) else None,
        "reference_dir": ref if os.path.isdir(ref) else None,
        "tts_python":    tts_python,
        "pool_repo":     REPO,
        "gpu":           gpu,
        "live_synth":    live,
    })
    saved = hc.save_config(cfg)

    print("HAL voice config written to", hc.CONFIG_PATH)
    print(f"  user_name     : {saved['user_name']}")
    print(f"  pool_dir      : {saved['pool_dir']}")
    print(f"  reference_dir : {saved['reference_dir']}")
    print(f"  pool_repo     : {saved['pool_repo']}")
    print(f"  tts_python    : {saved['tts_python'] or '(none - pool only)'}")
    print(f"  gpu           : {saved['gpu']}")
    print(f"  live synth    : {'ON (synthesizes new lines live)' if live else 'OFF (pool only)'}")
    n = len(hc.load_pool(saved['pool_dir']))
    print(f"  pool lines    : {n}")


if __name__ == "__main__":
    main()
