"""
Shared test harness for the Session HUD.

These tests exercise the real modules, but nothing about the machine they run on: they build a
synthetic Claude Code session registry, a scratch state directory, and fake transcripts, so a run
is identical on a laptop with twelve chats open and on a CI box with none. Nothing is spawned and
nothing on screen is touched - every process launch is stubbed out.

Run them all with:  python plugins/hal-voice/tests/run_all.py
"""
import json, os, shutil, sys, tempfile, time

SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
sys.path.insert(0, SCRIPTS)

import hal_common as hc          # noqa: E402
import hal_badge as hb           # noqa: E402
import hal_sessions as hs        # noqa: E402


def _own_proc_start():
    """This process's creation time, so a synthetic registry entry passes the PID-liveness check.

    `_pid_alive` accepts a PID whose recorded creation time matches the running process, and only
    falls back to sniffing the image name when it has no creation time to compare. Handing it ours
    is what lets a test pretend to be a Claude Code session without one being open."""
    if os.name != "nt":
        return None
    try:
        alive, created, _ = hs._win_proc(os.getpid())
        return created if alive and created else None
    except Exception:
        return None


class Sandbox(object):
    """A throwaway world: scratch state dir, fake registry, fake transcripts, no side effects."""

    def __init__(self, config=None):
        self.root = tempfile.mkdtemp(prefix="hud-test-")
        self.badges = os.path.join(self.root, "badges")
        self.registry = os.path.join(self.root, "sessions")
        self.projects = os.path.join(self.root, "projects")
        self.windows = os.path.join(self.root, "windows")
        for d in (self.badges, self.registry, self.projects, self.windows):
            os.makedirs(d, exist_ok=True)
        self.spawns = []                 # every badge window we would have launched
        self.alive = set()               # sids whose badge window we pretend is running
        self._saved = {}
        self._config = dict(hc._DEFAULTS)
        if config:
            self._config.update(config)
        self._patch()

    def _set(self, mod, name, value):
        self._saved.setdefault((mod, name), getattr(mod, name))
        setattr(mod, name, value)

    def _patch(self):
        self._set(hb, "BADGE_DIR", self.badges)
        self._set(hb, "SLOTS_PATH", os.path.join(self.root, "slots.json"))
        self._set(hb, "RECONCILE_MARK", os.path.join(self.badges, "reconcile.beat"))
        self._set(hs, "SESSIONS_DIR", self.registry)
        self._set(hs, "PROJECTS_DIR", self.projects)
        self._set(hs, "WINDOWS_DIR", self.windows)
        self._set(hs, "ALIVE_FILE", os.path.join(self.badges, "sessions_daemon.alive"))
        self._set(hc, "load_config", lambda: dict(self._config))
        self._set(hb, "_alive_fresh", lambda sid: hb._sid8(sid) in self.alive)

        class _Proc(object):
            pid = 4242

        def _popen(args, **kw):
            a = [str(x) for x in args]
            if "-StateFile" in a:
                self.spawns.append(os.path.basename(a[a.index("-StateFile") + 1]))
            return _Proc()

        self._set(hb.subprocess, "Popen", _popen)
        self._set(hb, "_ensure_daemon", lambda: None)
        self._set(hb, "ensure_helpers", lambda cfg=None: None)
        self._set(hs, "_reported_windows", lambda: [])
        # No desktop: tests that care about windows stub these themselves.
        self._set(hb, "_find_chat_windows", lambda cwd: [])
        self._set(hb, "_window_chat_titles", lambda: {})
        self._set(hb, "_is_vscode_window", lambda h: False)
        self._set(hb, "_git_branch", lambda cwd: "")
        # Naming must never reach the network or launch a CLI.
        self._set(hb, "_llm_run", lambda prompt, max_tokens=16: None)

    def close(self):
        for (mod, name), value in self._saved.items():
            setattr(mod, name, value)
        shutil.rmtree(self.root, ignore_errors=True)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    # ── building a world ──────────────────────────────────────────────────────
    def add_session(self, sid, cwd, pid=None, started=None, kind="interactive"):
        """Register a chat as open. Uses this process's identity so it reads as genuinely alive."""
        entry = {"sessionId": sid, "pid": pid or os.getpid(), "cwd": cwd, "kind": kind,
                 "startedAt": started if started is not None else int(time.time() * 1000)}
        ps = _own_proc_start()
        if ps and (pid is None or pid == os.getpid()):
            entry["procStart"] = str(ps)
        # Claude Code names these <pid>.json, but every synthetic chat borrows THIS process's pid
        # (that's what makes it read as alive), so one file per pid would let each new chat
        # overwrite the last and no test could ever have two open at once. `discover` reads the
        # sessionId out of the file and never looks at its name, so widening it is free - and a
        # chat re-registered under a new pid still gets its own entry, which is what resume does.
        name = "%s-%s.json" % (entry["pid"], hb._sid8(sid))
        with open(os.path.join(self.registry, name), "w", encoding="utf-8") as f:
            json.dump(entry, f)
        return entry

    def drop_session(self, sid):
        for name in os.listdir(self.registry):
            p = os.path.join(self.registry, name)
            try:
                if json.load(open(p, encoding="utf-8")).get("sessionId") == sid:
                    os.remove(p)
            except Exception:
                pass

    def add_transcript(self, sid, cwd, user_texts=(), ai_title=None):
        """A minimal transcript: enough for naming and for the ai-title window match."""
        d = os.path.join(self.projects, hs._proj_slug(cwd))
        os.makedirs(d, exist_ok=True)
        lines = []
        for t in user_texts:
            lines.append(json.dumps({"type": "user", "message": {"role": "user", "content": t},
                                     "timestamp": "2026-01-01T00:00:00.000Z"}))
        if ai_title:
            lines.append(json.dumps({"type": "ai-title", "aiTitle": ai_title, "sessionId": sid}))
        p = os.path.join(d, "%s.jsonl" % sid)
        with open(p, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + ("\n" if lines else ""))
        return p

    def report_window(self, active, tabs, folder="", ts=None):
        """Stand in for the companion VS Code extension's report of what a window is holding."""
        rep = {"ts": ts if ts is not None else hs._now(), "pid": 1234, "folder": folder,
               "active": active, "tabs": list(tabs)}
        self._set(hs, "_reported_windows", lambda: [rep])
        return rep

    # ── looking at the result ────────────────────────────────────────────────
    def tabs(self):
        return sorted(os.path.basename(f)[:-5] for f in os.listdir(self.badges)
                      if f.endswith(".json") and not f.startswith("reconcile"))

    def state(self, sid):
        return hb._read_state(sid)

    def age_tab(self, sid, ms):
        st = hb._read_state(sid)
        st["ts"] = st.get("ts", 0) - ms
        hb._write_state(sid, st)


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)
    return True
