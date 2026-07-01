#!/usr/bin/env python3
"""Mute / unmute the HAL voice plugin on this machine (config `muted`).

Usage:  hal_mute.py [on|off|toggle]   (default: toggle)

Muting silences both the spoken announcements and the on-screen popups; the hooks read
the flag live, so no reload is needed. Any python works (no ML stack required).
"""
import os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hal_common as hc

ON  = {"on", "mute", "muted", "1", "true", "yes"}
OFF = {"off", "unmute", "0", "false", "no"}


def main():
    arg = (sys.argv[1].lower() if len(sys.argv) > 1 else "toggle")
    cfg = hc.load_config()
    if arg in ON:
        muted = True
    elif arg in OFF:
        muted = False
    else:
        muted = not bool(cfg.get("muted"))
    cfg["muted"] = muted
    hc.save_config(cfg)
    print("HAL is now MUTED - silent until /hal-unmute." if muted
          else "HAL is now UNMUTED - it will speak again.")


if __name__ == "__main__":
    main()
