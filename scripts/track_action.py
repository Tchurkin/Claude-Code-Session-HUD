#!/usr/bin/env python3
"""PostToolUse hook: reads Claude's tool call from stdin, appends rich context to action file."""
import sys, json, os, re, subprocess
from datetime import datetime, timezone

ACTION_FILE  = os.path.join(os.path.expanduser("~"), ".claude", "last_claude_action.json")
PYTHON       = r"C:\Users\braxt\AppData\Local\Programs\Python\Python312\python.exe"
STATUS_POPUP = os.path.join(os.path.dirname(__file__), "status_popup.ps1")
PID_FILE     = os.path.join(os.path.expanduser("~"), ".claude", "status_popup_pid.txt")
STALE_SECONDS = 600


def kill_status_popup():
    try:
        if os.path.exists(PID_FILE):
            pid = int(open(PID_FILE).read().strip())
            subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                           capture_output=True, creationflags=0x08000000)
            try: os.remove(PID_FILE)
            except Exception: pass
    except Exception:
        pass


def load_state():
    try:
        data = json.loads(open(ACTION_FILE).read())
        ts = datetime.fromisoformat(data["ts"].replace("Z", "+00:00"))
        if (datetime.now(timezone.utc) - ts).total_seconds() < STALE_SECONDS:
            return data
    except Exception:
        pass
    return {"actions": [], "files": [], "cmds": [], "commit_msgs": [], "scripts": [], "ts": ""}


def save_state(state):
    os.makedirs(os.path.dirname(ACTION_FILE), exist_ok=True)
    state["ts"] = datetime.now(timezone.utc).isoformat()
    with open(ACTION_FILE, "w") as f:
        json.dump(state, f)


def script_to_status(name):
    """Turn a script filename into a human-readable status label."""
    n = name.lower().replace("_", " ").replace("-", " ")
    if any(k in n for k in ("sim", "simulation", "rocket", "tvc", "pid", "lqr", "adrc")):
        return "RUNNING SIMULATION"
    if any(k in n for k in ("exp", "experiment", "run exp")):
        return "RUNNING EXPERIMENT"
    if any(k in n for k in ("train", "fit", "regress", "model")):
        return "TRAINING MODEL"
    if any(k in n for k in ("plot", "viz", "visual", "chart", "graph", "figure")):
        return "GENERATING PLOTS"
    if any(k in n for k in ("test", "valid", "check", "audit")):
        return "RUNNING VALIDATION"
    if any(k in n for k in ("analys", "compute", "calc", "sweep", "scan")):
        return "ANALYZING RESULTS"
    if any(k in n for k in ("download", "fetch", "scrape", "pull")):
        return "FETCHING DATA"
    if any(k in n for k in ("build", "compile", "install", "setup")):
        return "BUILDING PROJECT"
    return f"RUNNING {os.path.splitext(name)[0].upper()[:28]}"


def file_to_status(path):
    ext  = os.path.splitext(path)[1].lower()
    name = os.path.basename(path).lower()
    if name in ("claude.md", "readme.md"):
        return "UPDATING DOCS"
    if "paper" in name or "draft" in name or ext in (".tex", ".rst"):
        return "WRITING PAPER"
    if ext == ".py":
        return "WRITING CODE"
    if ext == ".m":
        return "WRITING MATLAB"
    if ext == ".md":
        return "WRITING DOCS"
    if ext == ".ipynb":
        return "WRITING NOTEBOOK"
    if ext in (".json", ".toml", ".yaml", ".cfg", ".ini"):
        return "UPDATING CONFIG"
    if ext in (".csv", ".parquet", ".pkl", ".npy"):
        return "SAVING DATA"
    return "WRITING FILE"


def show_status(text):
    """Kill any existing status popup, then show a new brief one."""
    kill_status_popup()
    try:
        subprocess.Popen(
            ["powershell", "-ExecutionPolicy", "Bypass", "-NonInteractive",
             "-File", STATUS_POPUP, "-Text", text, "-DurationMs", "300000"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=0x08000000
        )
    except Exception:
        pass


def classify_bash(cmd):
    """Return (action_tag, detail_string, status_label_or_None)"""
    m = re.search(r"git\s+commit.*?-m\s+['\"](.+?)['\"]", cmd, re.DOTALL)
    if m:
        msg = m.group(1).strip()[:60]
        return "git_commit", msg, f"COMMITTING  {msg[:32]}"
    if re.search(r"git\s+push", cmd):
        return "git_push", None, "PUSHING TO REMOTE"
    if re.search(r"pytest|unittest|run_test", cmd):
        return "tests_ran", None, "RUNNING TESTS"
    m = re.search(r"python(?:3)?\s+([\w/\\.-]+\.py)", cmd)
    if m:
        script = os.path.basename(m.group(1))
        return "python_ran", script, script_to_status(script)
    m2 = re.search(r"python\s+-m\s+(\S+)", cmd)
    if m2:
        mod = m2.group(1)
        return "python_ran", mod, f"RUNNING {mod.upper()[:30]}"
    if re.search(r"matlab|octave", cmd, re.IGNORECASE):
        return "matlab_ran", None, "RUNNING MATLAB"
    if re.search(r"pip\s+install|conda\s+install", cmd):
        return "deps_installed", None, "INSTALLING DEPENDENCIES"
    if re.search(r"npm\s+|yarn\s+|cargo\s+build|make\b|cmake", cmd):
        return "bash_ran", None, "BUILDING PROJECT"
    # generic — no popup for minor shell ops
    short = cmd.strip().split("\n")[0][:80]
    return "bash_ran", short, None


def main():
    try:
        raw = sys.stdin.read()
        data = json.loads(raw)
    except Exception:
        return

    tool = data.get("tool_name", "")
    inp  = data.get("tool_input", {})

    state = load_state()
    status_label = None

    if tool == "Bash":
        cmd = inp.get("command", "")
        action, detail, status_label = classify_bash(cmd)
        if action not in state["actions"]:
            state["actions"].append(action)
        if action == "git_commit" and detail:
            if detail not in state["commit_msgs"]:
                state["commit_msgs"].append(detail)
        elif action == "python_ran" and detail:
            if detail not in state["scripts"]:
                state["scripts"].append(detail)
        elif detail and action not in ("git_commit", "python_ran"):
            if detail not in state["cmds"]:
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
                else "data_written" if os.path.splitext(path)[1].lower() in (".csv",".json",".pkl",".npy",".npz",".parquet")
                else "file_written"
            )
            if action not in state["actions"]:
                state["actions"].append(action)
            status_label = file_to_status(path)

    if status_label:
        show_status(status_label)

    save_state(state)


if __name__ == "__main__":
    main()
