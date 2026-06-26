#!/usr/bin/env python3
"""PreToolUse hook: shows a loading status popup BEFORE long bash commands run."""
import sys, json, os, re, subprocess

STATUS_POPUP = os.path.join(os.path.dirname(__file__), "status_popup.ps1")
PID_FILE     = os.path.join(os.path.expanduser("~"), ".claude", "status_popup_pid.txt")


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

# label → display duration in seconds (popup stays up while task runs)
LONG_TASKS = {
    "RUNNING SIMULATION":       90,
    "RUNNING EXPERIMENT":       60,
    "TRAINING MODEL":           90,
    "ANALYZING RESULTS":        30,
    "RUNNING TESTS":            30,
    "RUNNING MATLAB":           60,
    "GENERATING PLOTS":         20,
    "RUNNING VALIDATION":       30,
    "INSTALLING DEPENDENCIES":  35,
    "BUILDING PROJECT":         45,
    "FETCHING DATA":            20,
    "RUNNING CORRECTION":       60,
    "RUNNING FRONTIER":         60,
}


def script_to_status(name):
    n = name.lower().replace("_", " ").replace("-", " ")
    if any(k in n for k in ("sim", "simulation", "rocket", "tvc", "pid", "lqr", "adrc", "smc", "mpc")):
        return "RUNNING SIMULATION"
    if any(k in n for k in ("exp", "experiment")):
        return "RUNNING EXPERIMENT"
    if any(k in n for k in ("train", "fit", "regress")):
        return "TRAINING MODEL"
    if any(k in n for k in ("plot", "viz", "visual", "chart", "graph", "figure")):
        return "GENERATING PLOTS"
    if any(k in n for k in ("test", "valid", "audit")):
        return "RUNNING VALIDATION"
    if any(k in n for k in ("analys", "compute", "sweep", "scan", "frontier", "window", "margin")):
        return "ANALYZING RESULTS"
    if any(k in n for k in ("correction", "reclassif", "rerun")):
        return "RUNNING CORRECTION"
    if any(k in n for k in ("download", "fetch", "clone")):
        return "FETCHING DATA"
    if any(k in n for k in ("build", "compile", "setup")):
        return "BUILDING PROJECT"
    return None


def classify_cmd(cmd):
    if re.search(r"pytest|unittest|run_test", cmd):
        return "RUNNING TESTS", 30
    if re.search(r"matlab|octave", cmd, re.IGNORECASE):
        return "RUNNING MATLAB", 60
    if re.search(r"pip\s+install|conda\s+install", cmd):
        return "INSTALLING DEPENDENCIES", 35
    if re.search(r"npm\s+run|yarn\s+build|cargo\s+build|make\b", cmd):
        return "BUILDING PROJECT", 45
    m = re.search(r"python(?:3)?\s+([\w/\\.-]+\.py)", cmd)
    if m:
        script  = os.path.basename(m.group(1))
        label   = script_to_status(script)
        if label:
            return label, LONG_TASKS.get(label, 30)
    return None, 0


def main():
    try:
        raw  = sys.stdin.read()
        data = json.loads(raw)
    except Exception:
        return

    if data.get("tool_name") != "Bash":
        return

    cmd = data.get("tool_input", {}).get("command", "")
    label, duration_s = classify_cmd(cmd)
    if not label:
        return

    kill_status_popup()
    try:
        subprocess.Popen(
            ["powershell", "-ExecutionPolicy", "Bypass", "-NonInteractive",
             "-File", STATUS_POPUP,
             "-Text", label,
             "-DurationMs", "300000",   # stays until killed by next event
             "-Loading"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=0x08000000,
        )
    except Exception:
        pass


if __name__ == "__main__":
    main()
