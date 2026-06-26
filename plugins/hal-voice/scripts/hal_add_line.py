#!/usr/bin/env python3
"""Synthesize ONE HAL line with F5-TTS and add it to the pool (idempotent).

Routes through the warm daemon when possible so it reuses the already-loaded model
instead of loading a second copy (which could exhaust GPU memory). Falls back to an
in-process cold synth if no daemon / venv is available.

Usage:  python hal_add_line.py "<text>" [--play]
Run it with the configured tts_python (the f5-tts venv).
"""
import sys, os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hal_common as hc
import hal_tts_f5


def main():
    args = [a for a in sys.argv[1:] if a != "--play"]
    play = "--play" in sys.argv
    if not args or not args[0].strip():
        print("usage: hal_add_line.py \"<text>\" [--play]")
        return
    text = args[0].strip()
    cfg = hc.load_config()

    # Prefer the warm daemon (blocking through a cold model load); fall back to direct synth.
    path, dur = hal_tts_f5.request_synth(text, budget_ms=180000, cfg=cfg, allow_cold_wait=True)
    if not path:
        path, dur = hal_tts_f5.synth_and_pool(text, cfg)
    if not path:
        print("synthesis failed (no f5 venv / reference / ffmpeg?)")
        return

    print(f"ok -> {path} ({dur} ms)")
    if play:
        hc.play_audio(path, wait=True, timeout=max(15, (dur or 6000) // 1000 + 8))


if __name__ == "__main__":
    main()
