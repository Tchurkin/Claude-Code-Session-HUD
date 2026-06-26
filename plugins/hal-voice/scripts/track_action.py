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


def file_to_status(path):
    ext  = os.path.splitext(path)[1].lower()
    name = os.path.basename(path).lower()
    if name in ("claude.md", "readme.md"):                   return "UPDATING DOCS"
    if "paper" in name or "draft" in name or ext in (".tex", ".rst"): return "WRITING PAPER"
    if ext == ".py":     return "WRITING CODE"
    if ext == ".m":      return "WRITING MATLAB"
    if ext == ".md":     return "WRITING DOCS"
    if ext == ".ipynb":  return "WRITING NOTEBOOK"
    if ext in (".json", ".toml", ".yaml", ".cfg", ".ini"):   return "UPDATING CONFIG"
    if ext in (".csv", ".parquet", ".pkl", ".npy"):          return "SAVING DATA"
    return "WRITING FILE"


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

    tool = data.get("tool_name", "")
    inp  = data.get("tool_input", {})
    state = load_state()
    status_label = None

    if tool == "Bash":
        action, detail, status_label = classify_bash(inp.get("command", ""))
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
            status_label = file_to_status(path)

    if status_label:
        hc.show_status_popup(status_label, loading=False)
    save_state(state)


if __name__ == "__main__":
    main()
