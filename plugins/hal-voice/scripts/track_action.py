#!/usr/bin/env python3
"""PostToolUse hook: read Claude's tool call from stdin, record rich context for the
Stop-hook announcer, and surface a brief on-screen status popup."""
import sys, json, os, re
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hal_common as hc

STALE_SECONDS = 600


def load_state():
    try:
        data = json.loads(open(hc.ACTION_FILE, encoding="utf-8").read())
        ts = datetime.fromisoformat(data["ts"].replace("Z", "+00:00"))
        if (datetime.now(timezone.utc) - ts).total_seconds() < STALE_SECONDS:
            return data
    except Exception:
        pass
    return {"actions": [], "files": [], "cmds": [], "commit_msgs": [], "scripts": [], "ts": ""}


def save_state(state):
    hc.ensure_data_dir()
    state["ts"] = datetime.now(timezone.utc).isoformat()
    tmp = hc.ACTION_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f)
    os.replace(tmp, hc.ACTION_FILE)


def script_to_status(name):
    n = name.lower().replace("_", " ").replace("-", " ")
    if any(k in n for k in ("sim", "simulation", "rocket", "tvc", "pid", "lqr", "adrc")):
        return "RUNNING SIMULATION"
    if any(k in n for k in ("exp", "experiment", "run exp")):       return "RUNNING EXPERIMENT"
    if any(k in n for k in ("train", "fit", "regress", "model")):   return "TRAINING MODEL"
    if any(k in n for k in ("plot", "viz", "visual", "chart", "graph", "figure")): return "GENERATING PLOTS"
    if any(k in n for k in ("test", "valid", "check", "audit")):    return "RUNNING VALIDATION"
    if any(k in n for k in ("analys", "compute", "calc", "sweep", "scan")): return "ANALYZING RESULTS"
    if any(k in n for k in ("download", "fetch", "scrape", "pull")): return "FETCHING DATA"
    if any(k in n for k in ("build", "compile", "install", "setup")): return "BUILDING PROJECT"
    return f"RUNNING {os.path.splitext(name)[0].upper()[:28]}"


def bash_done_label(cmd, failed):
    """Descriptive 'what just finished' text for a bash command, with success/failure
    wording. Returns None for trivial shell ops (no toast)."""
    m = re.search(r"git\s+commit.*?-m\s+['\"](.+?)['\"]", cmd, re.DOTALL)
    if m:
        msg = m.group(1).strip()
        return "Commit failed" if failed else f"Committed: {msg[:32]}"
    if re.search(r"\bgit\s+push", cmd):              return "Push failed" if failed else "Pushed to remote"
    if re.search(r"\bgit\s+(pull|fetch)", cmd):      return "Update failed" if failed else "Updated from remote"
    if re.search(r"\bpytest\b|\bunittest\b|\bnose2?\b|run_test", cmd):
        return "Tests failed" if failed else "Tests passed"
    if re.search(r"pip\s+install|conda\s+install|poetry\s+add|npm\s+install|npm\s+ci|yarn\s+add", cmd):
        return "Install failed" if failed else "Dependencies installed"
    if re.search(r"npm\s+run|yarn\s+build|pnpm|cargo\s+build|\bmake\b|cmake|tsc\b|webpack|vite\s+build|docker\s+build", cmd):
        return "Build failed" if failed else "Build succeeded"
    if re.search(r"matlab|octave", cmd, re.IGNORECASE):
        return "MATLAB failed" if failed else "MATLAB finished"
    m = re.search(r"python(?:3)?(?:\.exe)?\s+(?:-u\s+)?(?:-m\s+([\w.]+)|([\w./\\-]+\.py))", cmd)
    if m:
        name = m.group(1) or os.path.basename(m.group(2))
        return f"{name} failed" if failed else f"Finished {name}"
    return None


def file_done_label(tool, path):
    """Descriptive 'what was just written' text, naming the file and its kind."""
    name = os.path.basename(path)
    ext  = os.path.splitext(name)[1].lower()
    verb = "Wrote" if tool == "Write" else "Edited"
    if name.lower() in ("claude.md", "readme.md") or ext in (".md", ".rst", ".tex"):
        return f"{verb} docs - {name}"
    if ext == ".ipynb":                                          return f"{verb} notebook - {name}"
    if ext in (".json", ".toml", ".yaml", ".yml", ".cfg", ".ini"): return f"{verb} config - {name}"
    if ext in (".csv", ".parquet", ".pkl", ".npy", ".npz"):      return f"Saved data - {name}"
    return f"{verb} {name}"


# Strong, low-false-positive failure / success markers in a command's output. Used to set
# `last_failed`, which steers the Stop hook toward an ominous vs. a triumphant HAL line.
FAIL_MARKERS = (
    "traceback (most recent call last)",
    "command not found", "is not recognized as", "no such file or directory",
    "fatal:", "error:", "errno", "modulenotfounderror", "syntaxerror",
    "segmentation fault", "assertionerror", "failed to ", " failed,",
)
PASS_MARKERS = ("passed", "build succeeded", "build success", "ok\n", "successfully ")


def _response_text(resp):
    """Best-effort flatten of a PostToolUse tool_response into searchable text."""
    if isinstance(resp, str):
        return resp
    if isinstance(resp, dict):
        return " ".join(str(resp.get(k, "")) for k in ("stdout", "stderr", "output", "error", "content"))
    return str(resp or "")


def detect_failure(data):
    """True/False if the tool result clearly failed/succeeded, else None (leave unchanged).
    Deliberately conservative: only flips the flag on unambiguous signals."""
    resp = data.get("tool_response")
    if isinstance(resp, dict) and resp.get("interrupted"):
        return True
    text = _response_text(resp).lower()
    if not text.strip():
        return None
    pytest_fail = re.search(r"\b\d+\s+failed\b", text)
    if pytest_fail or any(m in text for m in FAIL_MARKERS):
        return True
    if any(m in text for m in PASS_MARKERS):
        return False
    return None


def classify_bash(cmd):
    """Return (action_tag, detail_string, status_label_or_None)."""
    m = re.search(r"git\s+commit.*?-m\s+['\"](.+?)['\"]", cmd, re.DOTALL)
    if m:
        msg = m.group(1).strip()[:60]
        return "git_commit", msg, f"COMMITTING  {msg[:32]}"
    if re.search(r"git\s+push", cmd):                 return "git_push", None, "PUSHING TO REMOTE"
    if re.search(r"pytest|unittest|run_test", cmd):   return "tests_ran", None, "RUNNING TESTS"
    m = re.search(r"python(?:3)?\s+([\w/\\.-]+\.py)", cmd)
    if m:
        script = os.path.basename(m.group(1))
        return "python_ran", script, script_to_status(script)
    m2 = re.search(r"python\s+-m\s+(\S+)", cmd)
    if m2:
        return "python_ran", m2.group(1), f"RUNNING {m2.group(1).upper()[:30]}"
    if re.search(r"matlab|octave", cmd, re.IGNORECASE):       return "matlab_ran", None, "RUNNING MATLAB"
    if re.search(r"pip\s+install|conda\s+install", cmd):      return "deps_installed", None, "INSTALLING DEPENDENCIES"
    if re.search(r"npm\s+|yarn\s+|cargo\s+build|make\b|cmake", cmd): return "bash_ran", None, "BUILDING PROJECT"
    return "bash_ran", cmd.strip().split("\n")[0][:80], None   # minor shell op -> no popup


def main():
    try:
        data = json.loads(sys.stdin.read().lstrip("﻿"))  # tolerate a stray BOM
    except Exception:
        return
    try:
        import hal_badge; hal_badge.touch(data.get("session_id"), data.get("cwd"), state="working")
    except Exception:
        pass

    tool = data.get("tool_name", "")
    inp  = data.get("tool_input", {})
    state = load_state()
    status_label = None
    fail_now = False

    if tool == "Bash":
        # Remember whether this command's result clearly failed (recency wins, so a turn
        # that ends green clears an earlier red). The Stop hook reads this for its tone.
        # Scoped to Bash so a file's contents (Write/Edit echoes) can't trip it.
        failed = detect_failure(data)
        if failed is not None:
            state["last_failed"] = failed
        fail_now = failed is True

        cmd = inp.get("command", "")
        action, detail, _ = classify_bash(cmd)
        status_label = bash_done_label(cmd, fail_now)
        if action not in state["actions"]:
            state["actions"].append(action)
        if action == "git_commit" and detail and detail not in state["commit_msgs"]:
            state["commit_msgs"].append(detail)
        elif action == "python_ran" and detail and detail not in state["scripts"]:
            state["scripts"].append(detail)
        elif detail and action not in ("git_commit", "python_ran") and detail not in state["cmds"]:
            state["cmds"].append(detail)

    elif tool in ("Write", "Edit", "NotebookEdit"):
        path = inp.get("file_path", "")
        if path:
            name = os.path.basename(path)
            if name not in state["files"]:
                state["files"].append(name)
            action = (
                "paper_written" if ("paper" in name.lower() or name.lower() in ("claude.md", "readme.md"))
                else "python_written" if path.endswith(".py")
                else "matlab_written" if path.endswith(".m")
                else "docs_written" if path.endswith(".md")
                else "data_written" if os.path.splitext(path)[1].lower() in (".csv", ".json", ".pkl", ".npy", ".npz", ".parquet")
                else "file_written"
            )
            if action not in state["actions"]:
                state["actions"].append(action)
            status_label = file_done_label(tool, path)

    if status_label and not hc.is_muted():
        # Failures glare red; everything else wears this chat's color. Done states are
        # brief toasts (the in-progress popup is the long-lived one).
        sid = data.get("session_id")
        accent = hc.FAIL_COLOR if fail_now else hc.session_color(sid)
        hc.show_status_popup(status_label, loading=False, duration_ms=5000, accent=accent, session_id=sid)
    save_state(state)


if __name__ == "__main__":
    main()
