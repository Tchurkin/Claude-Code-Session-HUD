#!/usr/bin/env python3
"""PreToolUse hook: show a descriptive loading popup BEFORE a long bash command runs,
tinted with this chat's color (so two sessions are easy to tell apart)."""
import sys, json, os, re

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hal_common as hc


def script_topic(name):
    """A short present-tense phrase for what a script is probably doing (or None)."""
    n = name.lower().replace("_", " ").replace("-", " ")
    if any(k in n for k in ("sim", "rocket", "tvc", "trajectory", "orbit", "flight",
                            "pid", "lqr", "mpc", "adrc", "smc", "control")): return "Simulating"
    if any(k in n for k in ("train", "fit", "regress", "learn", "gp", "mlp", "nn")):  return "Training model"
    if any(k in n for k in ("plot", "viz", "visual", "chart", "graph", "figure", "fig")): return "Plotting"
    if any(k in n for k in ("test", "valid", "verify", "audit", "check")):            return "Validating"
    if any(k in n for k in ("analy", "compute", "calc", "sweep", "scan", "frontier", "margin")): return "Analyzing"
    if any(k in n for k in ("download", "fetch", "scrape", "ingest", "load data")):   return "Fetching data"
    if any(k in n for k in ("build", "compile", "render", "setup")):                  return "Building"
    if any(k in n for k in ("exp", "experiment", "run")):                             return "Running experiment"
    return None


def progress_label(cmd):
    """A descriptive 'currently doing X' label for a bash command, or None to stay quiet."""
    if re.search(r"\bpytest\b|\bunittest\b|\bnose2?\b|run_test", cmd):       return "Running tests"
    if re.search(r"matlab|octave", cmd, re.IGNORECASE):                     return "Running MATLAB"
    if re.search(r"pip\s+install|conda\s+install|poetry\s+add|npm\s+install|npm\s+ci|yarn\s+add", cmd):
        return "Installing dependencies"
    if re.search(r"npm\s+run|yarn\s+build|pnpm|cargo\s+build|\bmake\b|cmake|tsc\b|webpack|vite\s+build|docker\s+build", cmd):
        return "Building project"
    if re.search(r"\bgit\s+commit", cmd):                                   return "Committing changes"
    if re.search(r"\bgit\s+push", cmd):                                     return "Pushing to remote"
    if re.search(r"\bgit\s+(pull|fetch)", cmd):                             return "Updating from remote"
    if re.search(r"\bgit\s+clone|curl|wget|Invoke-WebRequest", cmd):        return "Fetching data"
    m = re.search(r"python(?:3)?(?:\.exe)?\s+(?:-u\s+)?(?:-m\s+([\w.]+)|([\w./\\-]+\.py))", cmd)
    if m:
        if m.group(1):
            return f"Running module {m.group(1)}"
        name  = os.path.basename(m.group(2))
        topic = script_topic(name)
        return f"{topic} - {name}" if topic else f"Running {name}"
    return None


def main():
    try:
        data = json.loads(sys.stdin.read().lstrip("﻿"))  # tolerate a stray BOM
    except Exception:
        return
    try:
        import hal_badge; hal_badge.touch(data.get("session_id"), data.get("cwd"), state="working")
    except Exception:
        pass
    if data.get("tool_name") != "Bash" or hc.is_muted():
        return
    label = progress_label(data.get("tool_input", {}).get("command", ""))
    if label:
        sid = data.get("session_id")
        hc.show_status_popup(label, loading=True, accent=hc.session_color(sid), session_id=sid)


if __name__ == "__main__":
    main()
