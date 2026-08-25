#!/usr/bin/env python3
"""
Run every HUD test suite.

    python plugins/hal-voice/tests/run_all.py

Each suite is a plain script that builds its own synthetic world (see _harness.py) and exits
non-zero on failure, so they run in a subprocess and are reported independently: one broken suite
tells you about the others too.
"""
import os, subprocess, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))


def main():
    suites = sorted(f for f in os.listdir(HERE) if f.startswith("test_") and f.endswith(".py"))
    if not suites:
        print("no suites found in %s" % HERE)
        return 1
    width = max(len(s) for s in suites)
    failed = []
    for s in suites:
        t0 = time.time()
        r = subprocess.run([sys.executable, os.path.join(HERE, s)],
                           capture_output=True, text=True)
        ok = r.returncode == 0
        last = ""
        for line in (r.stdout or "").strip().splitlines()[::-1]:
            if line.strip():
                last = line.strip()
                break
        print("%-*s  %s  %5.1fs  %s" % (width, s, "pass" if ok else "FAIL",
                                        time.time() - t0, last[:96]))
        if not ok:
            failed.append(s)
            for stream in (r.stdout, r.stderr):
                for line in (stream or "").strip().splitlines()[-15:]:
                    print("      %s" % line)
    print("\n%d suite(s), %d passed, %d failed" % (len(suites), len(suites) - len(failed), len(failed)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
