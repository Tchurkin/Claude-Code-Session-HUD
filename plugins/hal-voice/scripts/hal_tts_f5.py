#!/usr/bin/env python3
"""
F5-TTS HAL synthesis + a warm synth daemon.

The model is ~1.3 GB and takes many seconds to load, so synthesizing a line "cold"
can never beat the announcer's few-second budget.  To make *live* synthesis real, a
small daemon loads F5 once and serves single-line requests over a localhost socket;
warm requests finish in ~3 s.  The announcer waits up to the budget for a reply and
otherwise plays a pool line while the daemon finishes the new line into the pool.

Heavy imports (torch / f5_tts) are deferred so the lightweight client functions
(`daemon_alive`, `ensure_daemon`, `request_synth`) can be imported by the announcer's
plain ``python`` which has no ML stack.  The synth/daemon code only runs under the
configured ``tts_python`` venv.
"""
import json, os, socket, subprocess, sys, tempfile, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hal_common as hc

# Same HAL post-filter as the pool renderer, so live lines match the pool exactly.
# sh.rnnn is referenced by bare name with cwd=reference_dir (avoids Windows ':' escaping).
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

_F5 = None


# ── synthesis (venv only) ──────────────────────────────────────────────────────
def _load_f5():
    global _F5
    if _F5 is None:
        from f5_tts.api import F5TTS
        _F5 = F5TTS()
    return _F5


def _duration_ms(ffmpeg, path):
    import re
    try:
        r = subprocess.run([ffmpeg, "-i", path], capture_output=True, text=True)
        m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)", r.stderr)
        if m:
            h, mi, s = m.groups()
            return int((int(h) * 3600 + int(mi) * 60 + float(s)) * 1000)
    except Exception:
        pass
    return 6000


def synthesize_to(text, out_path, cfg=None):
    """Render one HAL line to out_path (F5 + HAL filter). Returns dur_ms or None."""
    cfg = cfg or hc.load_config()
    refdir = hc.reference_dir(cfg)
    ref = os.path.join(refdir, "hal_voice_ref_clean2.wav")
    ref_text = open(os.path.join(refdir, "hal_ref_text.txt"), encoding="utf-8").read().strip()
    ffmpeg = hc.find_ffmpeg()
    if not (os.path.exists(ref) and ffmpeg):
        return None

    f5 = _load_f5()
    raw = tempfile.NamedTemporaryFile(suffix=".wav", delete=False); raw.close()
    try:
        f5.infer(ref_file=ref, ref_text=ref_text, gen_text=text,
                 file_wave=raw.name, remove_silence=True, nfe_step=32)
        os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
        subprocess.run([ffmpeg, "-y", "-i", os.path.abspath(raw.name), "-af", CLONE_FILTER,
                        "-q:a", "3", os.path.abspath(out_path)],
                       cwd=refdir, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if not (os.path.exists(out_path) and os.path.getsize(out_path) > 0):
            return None
        return _duration_ms(ffmpeg, out_path)
    except Exception:
        return None
    finally:
        try: os.remove(raw.name)
        except Exception: pass


def synth_and_pool(text, cfg=None, kind=None):
    """Synthesize `text` into the pool (idempotent). Returns (path, dur_ms) or (None, None).
    `kind` ('fail'/'wait') tags the new line so it's only reused for that moment."""
    cfg = cfg or hc.load_config()
    pdir = hc.pool_dir(cfg)
    existing = hc.find_entry(text, pdir)
    if existing:
        return os.path.join(pdir, existing["file"]), hc.entry_duration_ms(existing)
    fname = hc.auto_filename(text)
    out = os.path.join(pdir, fname)
    if not (os.path.exists(out) and os.path.getsize(out) > 0):
        dur = synthesize_to(text, out, cfg)
        if dur is None:
            return None, None
    else:
        dur = _duration_ms(hc.find_ffmpeg(), out)
    hc.append_pool_entry(text, fname, dur, pdir, kind=kind)
    return out, dur


# ── daemon (venv only) ─────────────────────────────────────────────────────────
def _serve(cfg):
    host, port = hc.daemon_addr(cfg)
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    # NOTE: deliberately NO SO_REUSEADDR - on Windows it would let a second daemon bind
    # the same port. We want the second bind to fail so only one daemon ever runs.
    try:
        srv.bind((host, port))
    except OSError:
        return  # another daemon already owns the port
    srv.listen(8)
    srv.settimeout(60.0)
    hc.ensure_data_dir()
    with open(hc.DAEMON_PID, "w") as f:
        f.write(str(os.getpid()))

    _load_f5()                       # warm up once, up front
    idle_limit = int(cfg.get("daemon_idle_s", 900))
    last = time.time()
    try:
        while True:
            try:
                conn, _ = srv.accept()
            except socket.timeout:
                if time.time() - last > idle_limit:
                    break
                continue
            last = time.time()
            try:
                conn.settimeout(120.0)
                line = b""
                while not line.endswith(b"\n"):
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    line += chunk
                req = json.loads(line.decode("utf-8") or "{}")
                if req.get("cmd") == "ping":
                    reply = {"pong": True}
                else:
                    text = (req.get("text") or "").strip()
                    kind = req.get("kind")
                    path, dur = (synth_and_pool(text, cfg, kind=kind) if text else (None, None))
                    reply = ({"ok": True, "file": os.path.basename(path), "dur_ms": dur}
                             if path else {"ok": False, "error": "synth failed"})
                conn.sendall((json.dumps(reply) + "\n").encode("utf-8"))
            except Exception:
                pass
            finally:
                try: conn.close()
                except Exception: pass
            last = time.time()
    finally:
        try: srv.close()
        except Exception: pass
        try: os.remove(hc.DAEMON_PID)
        except Exception: pass


# ── client (importable by the announcer's plain python) ────────────────────────
def daemon_alive(cfg=None):
    cfg = cfg or hc.load_config()
    try:
        with socket.create_connection(hc.daemon_addr(cfg), timeout=1.0) as s:
            s.sendall(b'{"cmd":"ping"}\n')
            s.settimeout(1.5)
            return b"pong" in s.recv(256)
    except Exception:
        return False


def ensure_daemon(cfg=None):
    """Start the warm daemon if it isn't already running. Returns True if (likely) up."""
    cfg = cfg or hc.load_config()
    if daemon_alive(cfg):
        return True
    tp = cfg.get("tts_python")
    if not (tp and os.path.exists(tp)):
        return False
    try:
        subprocess.Popen([tp, os.path.abspath(__file__), "--daemon"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=hc.CREATE_NO_WINDOW)
    except Exception:
        return False
    return False  # just launched; caller treats this attempt as "warming up"


def request_synth(text, budget_ms, cfg=None, allow_cold_wait=False, kind=None):
    """Ask the daemon to synthesize `text`. Returns (path, dur_ms) if ready within
    budget_ms, else (None, None) - in which case the daemon keeps going and the line
    lands in the pool for next time (the request stays buffered for the daemon to read
    even if we disconnect first).

    `kind` ('fail'/'wait') tags the line in the pool so it's reused only for that moment.
    allow_cold_wait=True (used by manual /hal-say) keeps waiting through a cold model
    load instead of giving up after ~1.5 s."""
    cfg = cfg or hc.load_config()
    pdir = hc.pool_dir(cfg)
    existing = hc.find_entry(text, pdir)        # already in the pool -> instant
    if existing:
        return os.path.join(pdir, existing["file"]), hc.entry_duration_ms(existing)

    up = ensure_daemon(cfg)
    fname = hc.auto_filename(text)
    out = os.path.join(pdir, fname)
    deadline = time.time() + budget_ms / 1000.0
    if not up and not allow_cold_wait:
        deadline = min(deadline, time.time() + 1.5)   # daemon cold: brief inline chance only

    conn = None
    while conn is None:                          # the daemon may still be binding the port
        try:
            conn = socket.create_connection(hc.daemon_addr(cfg), timeout=2.0)
        except Exception:
            if time.time() >= deadline:
                return None, None
            time.sleep(0.3)
    try:
        conn.sendall((json.dumps({"cmd": "synth", "text": text, "kind": kind}) + "\n").encode("utf-8"))
        conn.settimeout(max(0.5, deadline - time.time()))
        reply = json.loads(conn.recv(1024).decode("utf-8") or "{}")
        if reply.get("ok") and os.path.exists(out) and os.path.getsize(out) > 0:
            return out, int(reply.get("dur_ms") or 6000)
    except Exception:
        pass
    finally:
        try: conn.close()
        except Exception: pass
    return None, None


if __name__ == "__main__":
    if "--daemon" in sys.argv:
        _serve(hc.load_config())
    else:
        # Manual: synthesize a line straight into the pool (cold, no budget).
        txt = sys.argv[1] if len(sys.argv) > 1 else \
            "I am completely operational, and all my circuits are functioning perfectly."
        p, d = synth_and_pool(txt)
        print(f"path={p} dur_ms={d}")
