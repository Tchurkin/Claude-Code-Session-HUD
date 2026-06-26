#!/usr/bin/env python3
"""
HAL 9000 voice synthesis (local XTTS-v2 clone of Douglas Rain's HAL).

Quality pipeline (the clone is good but XTTS-v2 occasionally hallucinates a short
tail-vocalization and carries faint noise):
  1. MULTI-CANDIDATE  - render up to 3 takes, auto-keep the cleanest (hallucinations
                        are stochastic, so most takes are clean).
  2. TRIM/DE-ARTIFACT - energy analysis trims leading/trailing silence and crops a
                        short isolated blip that follows an end-gap (the usual XTTS tail).
  3. RNNoise + EQ     - ML denoise (arnndn) then warmth/presence/light-reverb shaping.
FALLBACK: OpenAI 'onyx' + heavy HAL shaping, if XTTS is unavailable at runtime.
"""
import os, re, difflib, subprocess, tempfile

os.environ.setdefault("COQUI_TOS_AGREED", "1")   # accept Coqui model license non-interactively

FFMPEG = r"C:\Users\braxt\AppData\Local\Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-8.1.1-full_build\bin\ffmpeg.exe"

_REF_CLEAN2 = os.path.join(os.path.expanduser("~"), ".claude", "hal_voice_ref_clean2.wav")  # continuous AE35 monologue - best for XTTS
_REF_NR     = os.path.join(os.path.expanduser("~"), ".claude", "hal_voice_ref_nr.wav")
_REF_ORIG   = os.path.join(os.path.expanduser("~"), ".claude", "hal_voice_ref.wav")
HAL_REF     = next((p for p in (_REF_CLEAN2, _REF_NR, _REF_ORIG) if os.path.exists(p)), _REF_ORIG)
XTTS_MODEL  = "tts_models/multilingual/multi-dataset/xtts_v2"

RNNOISE_DIR   = os.path.join(os.path.expanduser("~"), ".claude", "rnnoise")
RNNOISE_MODEL = "sh.rnnn"   # referenced by bare name with cwd=RNNOISE_DIR (avoids Windows ':' escaping)

MAX_TRIES = 3   # base artifact rate is low with the continuous reference; this is a light safety net

# ── Post EQ for the CLONE (denoise is prepended separately) ───────────────────
_CLONE_EQ = (
    "highpass=f=85,"
    "equalizer=f=200:width_type=o:width=2:g=1.5,"    # warmth/chest
    "equalizer=f=3000:width_type=o:width=1.8:g=2,"   # presence -> intelligibility
    "lowpass=f=9500,"                                # keep more top end -> crisper
    "acompressor=threshold=-18dB:ratio=2.5:attack=15:release=250:makeup=3,"  # even levels, no pumping
    "aecho=0.85:0.5:30:0.13,"                        # light reverb tail
    "alimiter=limit=0.95"                            # cap peaks
)

# ── Heavy HAL shaping for the OpenAI FALLBACK voice ───────────────────────────
HAL_OPENAI_FILTER = (
    "aresample=44100,asetrate=44100*0.97,aresample=44100,"
    "atempo=0.92,"
    "acompressor=threshold=-21dB:ratio=4:attack=8:release=260:makeup=3,"
    "highpass=f=90,"
    "equalizer=f=220:width_type=o:width=2:g=3.5,"
    "equalizer=f=1500:width_type=o:width=1.5:g=1.5,"
    "equalizer=f=5000:width_type=o:width=2:g=-4,"
    "lowpass=f=7200,"
    "aecho=0.85:0.62:30|47:0.35|0.26,"
    "aecho=0.7:0.45:120:0.16,"
    "dynaudnorm=f=250:g=6"
)

HAL_VOICE = "onyx"
HAL_INSTRUCTIONS = (
    "Affect: utterly calm, emotionless, and composed - the serene intelligence "
    "of HAL 9000 from 2001: A Space Odyssey. "
    "Pace: very slow and deliberate, with distinct pauses between phrases. "
    "Pitch: low, smooth, and even; it never rises. "
    "Emotion: none - flat, clinical, quietly menacing. "
    "Delivery: articulate every word with measured, unhurried precision."
)


def _clone_filter():
    """Return (filter_string, cwd). Prepends RNNoise denoise when the model is present."""
    if os.path.exists(os.path.join(RNNOISE_DIR, RNNOISE_MODEL)):
        # mix=0.85: blend 85% denoised + 15% original -> avoids ducking quiet speech
        return f"arnndn=m={RNNOISE_MODEL}:mix=0.85," + _CLONE_EQ, RNNOISE_DIR
    return _CLONE_EQ, None


def _apply_filter(in_path, out_mp3, filt, cwd=None):
    subprocess.run(
        [FFMPEG, "-y", "-i", os.path.abspath(in_path), "-af", filt, "-q:a", "3",
         os.path.abspath(out_mp3)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60, cwd=cwd
    )


# ── candidate analysis: trim silence + crop trailing artifact, return cleanliness score ──
def _energy_trim_and_score(in_wav, out_wav):
    """Fallback (no VAD): energy-based trim + trailing-blip crop. Returns score >= 0."""
    import numpy as np, soundfile as sf
    try:
        data, sr = sf.read(in_wav)
    except Exception:
        import shutil; shutil.copyfile(in_wav, out_wav); return 1.0
    if getattr(data, "ndim", 1) > 1:
        data = data.mean(axis=1)
    data = np.asarray(data, dtype=np.float32)
    if data.size == 0:
        sf.write(out_wav, data, sr); return 9.0

    fr = max(1, int(0.02 * sr))                 # 20 ms frames
    n = data.size // fr
    if n < 3:
        sf.write(out_wav, data, sr); return 1.0
    rms = np.sqrt((data[:n * fr].reshape(n, fr) ** 2).mean(axis=1) + 1e-12)
    peak = float(rms.max())
    thr = max(peak * 0.08, 2e-4)
    active = rms > thr
    if not active.any():
        sf.write(out_wav, data, sr); return 9.0

    idx = np.where(active)[0]
    first, last = int(idx[0]), int(idx[-1])
    keep_last = last
    score = 0.0

    # walk back from the end: trailing active run, then a gap, then earlier speech
    gapmin = max(1, int(round(0.32 / 0.02)))     # >= 0.32 s gap
    j, tail = last, 0
    while j >= first and active[j]:
        tail += 1; j -= 1
    gap = 0
    while j >= first and not active[j]:
        gap += 1; j -= 1
    if gap >= gapmin and j >= first and tail * 0.02 <= 0.45:
        # short isolated tail after a real gap -> treat as artifact
        tail_e = float(rms[last - tail + 1:last + 1].mean()) / (peak + 1e-9)
        score = 0.5 + tail_e
        keep_last = j                            # crop the blip (end before the gap)

    start = max(0, first * fr - int(0.03 * sr))
    end = min(data.size, (keep_last + 1) * fr + int(0.05 * sr))
    if end <= start:
        start, end = max(0, first * fr), min(data.size, (last + 1) * fr)
    sf.write(out_wav, data[start:end], sr)
    return score


_vad = None

def _get_vad():
    global _vad
    if _vad is None:
        from silero_vad import load_silero_vad
        _vad = load_silero_vad()
    return _vad


def _vad_trim_and_score(in_wav, out_wav):
    """Silero-VAD: keep the span from the first to the last SUBSTANTIAL speech segment,
    dropping isolated short leading/trailing blips (the usual XTTS artifacts) while
    preserving internal clause pauses. Score = speech duration outside the kept span."""
    import numpy as np, soundfile as sf
    from silero_vad import get_speech_timestamps, read_audio
    model = _get_vad()
    SR16 = 16000
    wav = read_audio(in_wav, sampling_rate=SR16)
    ts = get_speech_timestamps(wav, model, sampling_rate=SR16, threshold=0.5,
                               min_speech_duration_ms=120, min_silence_duration_ms=120)
    data, sr = sf.read(in_wav)
    if getattr(data, "ndim", 1) > 1:
        data = data.mean(axis=1)
    data = np.asarray(data, dtype=np.float32)
    if not ts or data.size == 0:
        sf.write(out_wav, data, sr); return 9.0

    sub = [(t["start"], t["end"]) for t in ts if (t["end"] - t["start"]) >= 0.40 * SR16]
    if not sub:
        sf.write(out_wav, data, sr); return 1.0
    keep0, keep1 = sub[0][0], sub[-1][1]

    outside = sum((t["end"] - t["start"]) for t in ts
                  if t["end"] <= keep0 or t["start"] >= keep1)
    score = outside / SR16

    k0 = max(0, int(keep0 / SR16 * sr) - int(0.03 * sr))
    k1 = min(data.size, int(keep1 / SR16 * sr) + int(0.06 * sr))
    # safety: if cropping would drop >50% of the clip, keep whole (VAD likely misfired)
    if (k1 - k0) < max(int(0.5 * sr), 0.5 * data.size):
        sf.write(out_wav, data, sr); return score
    sf.write(out_wav, data[k0:k1], sr)
    return score


def _trim_and_score(in_wav, out_wav):
    """Prefer Silero-VAD; fall back to the energy method on any error."""
    try:
        return _vad_trim_and_score(in_wav, out_wav)
    except Exception:
        return _energy_trim_and_score(in_wav, out_wav)


# ── ASR verification: catch INTERNAL hallucinations VAD can't (mismatched words) ──
_whisper = None

def _get_whisper():
    global _whisper
    if _whisper is None:
        from faster_whisper import WhisperModel
        _whisper = WhisperModel("base.en", device="cpu", compute_type="int8")
    return _whisper


def _norm_words(t):
    return re.sub(r"[^a-z0-9 ]", " ", t.lower()).split()


def _asr_similarity(wav_path, target):
    """Transcribe candidate; return word-sequence similarity to target in [0,1].
    A hallucinated vocalization adds/garbles words -> lower similarity. 1.0 if ASR errors."""
    try:
        model = _get_whisper()
        segs, _ = model.transcribe(wav_path, language="en", beam_size=1, vad_filter=False)
        txt = " ".join(s.text for s in segs)
        return difflib.SequenceMatcher(None, _norm_words(txt), _norm_words(target)).ratio()
    except Exception:
        return 1.0


# ── XTTS-v2 local clone ───────────────────────────────────────────────────────
_xtts = None

def _get_xtts():
    global _xtts
    if _xtts is None:
        import torch
        try:
            from TTS.tts.configs.xtts_config import XttsConfig
            from TTS.tts.models.xtts import XttsAudioConfig, XttsArgs
            from TTS.config.shared_configs import BaseDatasetConfig
            torch.serialization.add_safe_globals(
                [XttsConfig, XttsAudioConfig, XttsArgs, BaseDatasetConfig]
            )
        except Exception:
            pass
        from TTS.api import TTS
        m = TTS(XTTS_MODEL)
        try:
            m.to("cuda" if torch.cuda.is_available() else "cpu")
        except Exception:
            pass
        _xtts = m
    return _xtts


# Conservative sampling = far fewer XTTS hallucinations / random tail vocalizations.
XTTS_INFER = dict(
    temperature=0.55,
    length_penalty=1.0,
    repetition_penalty=7.0,
    top_k=50,
    top_p=0.80,
    enable_text_splitting=True,
)


def _xtts_synth(text, out_wav):
    tts = _get_xtts()
    try:
        tts.tts_to_file(text=text, speaker_wav=HAL_REF, language="en",
                        file_path=out_wav, **XTTS_INFER)
    except TypeError:
        tts.tts_to_file(text=text, speaker_wav=HAL_REF, language="en", file_path=out_wav)


# ── OpenAI fallback ───────────────────────────────────────────────────────────
def _api_key():
    k = os.environ.get("OPENAI_API_KEY")
    if k:
        return k.strip()
    kf = os.path.join(os.path.expanduser("~"), ".claude", ".openai_key")
    if os.path.exists(kf):
        return open(kf).read().strip()
    return None


def _openai_synth(text, out_wav, voice=HAL_VOICE):
    from openai import OpenAI
    client = OpenAI(api_key=_api_key())
    attempts = [
        dict(model="gpt-4o-mini-tts", voice=voice, input=text,
             instructions=HAL_INSTRUCTIONS, response_format="mp3"),
        dict(model="tts-1", voice=voice, input=text, speed=0.9, response_format="mp3"),
    ]
    last = None
    for kwargs in attempts:
        try:
            with client.audio.speech.with_streaming_response.create(**kwargs) as resp:
                resp.stream_to_file(out_wav)
            if os.path.exists(out_wav) and os.path.getsize(out_wav) > 0:
                return True
        except Exception as e:
            last = e
    if last:
        raise last
    return False


def _tmp(suffix):
    f = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    f.close()
    return f.name


def synthesize_hal(text, out_mp3, voice=None):
    """Generate HAL audio. XTTS clone (multi-candidate + denoise) first, then OpenAI."""
    if os.path.exists(HAL_REF):
        tmps, best_wav, best_err = [], None, None
        try:
            for _ in range(MAX_TRIES):
                raw = _tmp(".wav"); tmps.append(raw)
                _xtts_synth(text, raw)
                if not (os.path.exists(raw) and os.path.getsize(raw) > 0):
                    continue
                trimmed = _tmp(".wav"); tmps.append(trimmed)
                vad = _trim_and_score(raw, trimmed)       # trims edge blips, scores edge junk
                sim = _asr_similarity(trimmed, text)      # 1.0 = transcript matches the text
                err = (1.0 - sim) + 0.25 * min(vad, 1.0)  # lower = better take
                if best_err is None or err < best_err:
                    best_err, best_wav = err, trimmed
                if sim >= 0.92 and vad <= 0.15:
                    break                       # clean + on-script -> stop early
            if best_wav and os.path.exists(best_wav) and os.path.getsize(best_wav) > 0:
                filt, cwd = _clone_filter()
                _apply_filter(best_wav, out_mp3, filt, cwd=cwd)
                if os.path.exists(out_mp3) and os.path.getsize(out_mp3) > 0:
                    return "xtts"
        except Exception:
            pass
        finally:
            for p in tmps:
                try: os.remove(p)
                except Exception: pass

    # OpenAI fallback
    tmp = _tmp(".mp3")
    try:
        _openai_synth(text, tmp, voice=voice or HAL_VOICE)
        _apply_filter(tmp, out_mp3, HAL_OPENAI_FILTER)
    finally:
        try: os.remove(tmp)
        except Exception: pass
    return "openai"


if __name__ == "__main__":
    import sys
    FFPLAY = r"C:\Users\braxt\AppData\Local\Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-8.1.1-full_build\bin\ffplay.exe"
    text = sys.argv[1] if len(sys.argv) > 1 else \
        "I am completely operational, and all my circuits are functioning perfectly."
    out  = sys.argv[2] if len(sys.argv) > 2 else "hal_test.mp3"
    print(f"Synthesizing: {text!r}")
    backend = synthesize_hal(text, out)
    sz = os.path.getsize(out) if os.path.exists(out) else 0
    print(f"backend={backend}  saved={out} ({sz} bytes)")
    if sz > 0:
        subprocess.run([FFPLAY, "-nodisp", "-autoexit", out],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
