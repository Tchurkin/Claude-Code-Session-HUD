#!/usr/bin/env python3
"""PreToolUse hook: show a loading status popup BEFORE a long bash command runs."""
import sys, json, os, re

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hal_common as hc


def script_to_status(name):
    n = name.lower().replace("_", " ").replace("-", " ")
    if any(k in n for k in ("sim", "simulation", "rocket", "tvc", "pid", "lqr", "adrc", "smc", "mpc")):
        return "RUNNING SIMULATION"
    if any(k in n for k in ("exp", "experiment")):        return "RUNNING EXPERIMENT"
    if any(k in n for k in ("train", "fit", "regress")):  return "TRAINING MODEL"
    if any(k in n for k in ("plot", "viz", "visual", "chart", "graph", "figure")): return "GENERATING PLOTS"
    if any(k in n for k in ("test", "valid", "audit")):   return "RUNNING VALIDATION"
    if any(k in n for k in ("analys", "compute", "sweep", "scan", "frontier", "window", "margin")): return "ANALYZING RESULTS"
    if any(k in n for k in ("correction", "reclassif", "rerun")): return "RUNNING CORRECTION"
    if any(k in n for k in ("download", "fetch", "clone")): return "FETCHING DATA"
    if any(k in n for k in ("build", "compile", "setup")): return "BUILDING PROJECT"
    return None


def classify_cmd(cmd):
    if re.search(r"pytest|unittest|run_test", cmd):                  return "RUNNING TESTS"
    if re.search(r"matlab|octave", cmd, re.IGNORECASE):             return "RUNNING MATLAB"
    if re.search(r"pip\s+install|conda\s+install", cmd):            return "INSTALLING DEPENDENCIES"
    if re.search(r"npm\s+run|yarn\s+build|cargo\s+build|make\b", cmd): return "BUILDING PROJECT"
    m = re.search(r"python(?:3)?\s+([\w/\\.-]+\.py)", cmd)
    if m:
        return script_to_status(os.path.basename(m.group(1)))
    return None


def main():
    try:
        data = json.loads(sys.stdin.read().lstrip("﻿"))  # tolerate a stray BOM
    except Exception:
        return
    if data.get("tool_name") != "Bash":
        return
    label = classify_cmd(data.get("tool_input", {}).get("command", ""))
    if label:
        hc.show_status_popup(label, loading=True)


if __name__ == "__main__":
    main()
