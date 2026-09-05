"""Roster-pinned tab names, and the master spend level.

Both exist because the sessions stopped being isolated. They read each other's folders now, so a
name inferred from what a chat has been TALKING ABOUT drifts to the subject rather than the owner -
observed live: the College Apps chat, deep in resume content about rocketry, renamed itself "Rocket
Research" and collided with the chat that actually is that. A folder is a fact; a topic is not.

The spend level is the other half: one control that every session resolves through its own floor and
ceiling, so a safety-critical worker stays rigorous when it is turned down and a low-stakes one stops
burning tokens when it is turned up.

Everything here runs against a synthetic workspace. Nothing reads the real one.
"""
import json, os, shutil, sys, tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import check              # noqa: E402

import hal_badge as hb                  # noqa: E402

tmp = tempfile.mkdtemp(prefix="hud-budget-")
WS = os.path.join(tmp, "Projects")
SHARED = os.path.join(tmp, "shared")
os.makedirs(WS)
os.makedirs(SHARED)

# A stand-in for the coordinating session's team.py: the two names this reads are ROSTER and
# WORKSPACE, so the fake declares exactly those.
with open(os.path.join(SHARED, "team.py"), "w", encoding="utf-8") as f:
    f.write("from pathlib import Path\n")
    f.write("WORKSPACE = Path(r%r)\n" % WS)
    f.write("ROSTER = {\n"
            "  'counsellor': {'folder': 'College Apps'},\n"
            "  'portfolio':  {'folder': 'Engineering Portfolio'},\n"
            "  'hud':        {'folder': 'Claude-Code-Session-HUD-main'},\n"
            "  'researcher': {'folder': 'TVC PID Research'},\n"
            "  'engineer':   {'folder': 'TVC PID Research'},\n"
            "  'pm':         {'folder': '.'},\n"
            "}\n")

BUDGET = {
    "level": "lean",
    "levels": {
        "eco":      {"rank": 0, "label": "Eco", "effort": "low", "verify": "none", "policy": "Cheap."},
        "lean":     {"rank": 1, "label": "Lean", "effort": "low", "verify": "load-bearing-only", "policy": "Lean."},
        "balanced": {"rank": 2, "label": "Balanced", "effort": "medium", "verify": "own-work", "policy": "Middle."},
        "thorough": {"rank": 3, "label": "Thorough", "effort": "high", "verify": "cross-check", "policy": "Full."},
    },
    "agent_overrides": {
        "engineer":   {"floor": "balanced", "why": "pyro channels"},
        "portfolio":  {"ceiling": "lean", "why": "a website"},
        "counsellor": {"floor": "lean", "ceiling": "balanced"},
    },
    "pm_only_key": {"do": "not touch"},
}
BPATH = os.path.join(SHARED, "budget.json")
json.dump(BUDGET, open(BPATH, "w", encoding="utf-8"), indent=2)

_saved = (hb.SHARED_DIR, hb.BUDGET_PATH)
hb.SHARED_DIR, hb.BUDGET_PATH = SHARED, BPATH
hb._ROSTER_CACHE["mt"] = None
hb._BUDGET_CACHE["mt"] = None

J = os.path.join

# -- 1. a folder decides the name, and only when it can ---------------------------------------------
for folder, want in (("College Apps", "College Apps"),
                     ("Engineering Portfolio", "Engineering Portfolio"),
                     ("Claude-Code-Session-HUD-main", "Session HUD")):
    got = hb._pinned_label(J(WS, folder))
    check(got == want, "%s pins to %r (got %r)" % (folder, want, got))

check(hb._pinned_label(J(WS, "College Apps", "essays", "drafts")) == "College Apps",
      "a chat opened deeper in a project still belongs to it")

# The folder two agents share stays inferred: the path genuinely cannot say which one this is.
check(hb._pinned_label(J(WS, "TVC PID Research")) == "",
      "a folder holding two agents is not pinned - the path cannot disambiguate it")
check(hb._pinned_label(J(WS, "TVC PID Research", "Firmware")) == "", "nor is anything inside it")

# The root is the pm session, and matches EXACTLY. Prefix-matching it would hand "Workspace PM" to
# every unlisted folder underneath - including the two-agent one, which is worse than the drift.
check(hb._pinned_label(WS) == "Workspace PM", "the workspace root is the coordinating session")
check(hb._pinned_label(J(WS, "Some New Folder")) == "",
      "but an unlisted folder under it inherits nothing")
check(hb._pinned_label(J(tmp, "elsewhere")) == "", "and nothing outside the workspace is pinned")
check(hb._pinned_label("") == "" and hb._pinned_label(None) == "", "no cwd, no claim")
# And the naming path has to actually USE it. Everything above calls _pinned_label directly and
# would not notice _compute_topic quietly going back to asking Claude - which is the whole cost
# saving as well as the fix.
def _boom(*a, **k):
    raise AssertionError("_compute_topic asked Claude for a folder the roster already answers")


_llm = hb._llm_topic
hb._llm_topic = _boom
try:
    name, src = hb._compute_topic(None, cwd=J(WS, "College Apps"), session_id="deadbeef")
    check((name, src) == ("College Apps", "roster"),
          "a rostered folder is named from the roster, without asking (got %r)" % ((name, src),))
finally:
    hb._llm_topic = _llm
check(src == "roster", "and the source says so, so it is never mistaken for an inference")
print("names: pinned from the folder, except where a folder genuinely cannot say")


# -- 2. it must be entirely optional --------------------------------------------------------------
# This plugin is public. Almost nobody has a ~/.claude/shared, and for them nothing may change.
hb.SHARED_DIR = J(tmp, "no-such-dir")
hb._ROSTER_CACHE["mt"] = None
check(hb._roster() == {}, "no shared directory, no roster")
check(hb._pinned_label(J(WS, "College Apps")) == "", "and no pinning, so naming behaves as before")
hb.SHARED_DIR = SHARED
hb._ROSTER_CACHE["mt"] = None
check(hb._pinned_label(J(WS, "College Apps")) == "College Apps", "and it comes back when present")
print("optional: without the shared directory the plugin is unchanged")


# -- 3. the level resolves through each agent's own floor and ceiling ---------------------------------
def at(level):
    doc = dict(BUDGET); doc["level"] = level
    json.dump(doc, open(BPATH, "w", encoding="utf-8"), indent=2)
    hb._BUDGET_CACHE["mt"] = None
    out = {}
    for a in ("engineer", "portfolio", "counsellor", "hud", ""):
        pol = hb.budget_policy(a)
        out[a or "unknown"] = pol.split("Spend level: ")[1].split(" (")[0] if pol else ""
    return out


eco, thorough = at("eco"), at("thorough")
check(eco["engineer"] == "Balanced", "a floor holds a safety-critical agent up (got %r)" % eco["engineer"])
check(eco["counsellor"] == "Lean", "and holds counsellor at its floor (got %r)" % eco["counsellor"])
check(eco["portfolio"] == "Eco", "while an agent with only a ceiling follows the slider down")
check(thorough["portfolio"] == "Lean", "a ceiling holds a low-stakes agent down (got %r)" % thorough["portfolio"])
check(thorough["counsellor"] == "Balanced", "and counsellor stops at its ceiling")
check(thorough["engineer"] == "Thorough", "while a floor-only agent follows the slider up")
check(thorough["unknown"] == "Thorough", "an unrostered session just takes the master level")
check(at("lean")["engineer"] == "Balanced", "the floor still bites at the middle")

pol = hb.budget_policy("hud")
check(len(pol) < 600, "the line is short - it prepends to every turn (%d bytes)" % len(pol))
check("Spend level:" in pol and "effort" in pol and "verify" in pol,
      "and says the level, the effort and how much to verify (%r)" % pol[:60])
print("levels: floors and ceilings both bite, %d bytes on the wire" % len(pol))


# -- 4. writing the level must not touch anything else ------------------------------------------------
# Every other key belongs to the coordinating session. A slider that rewrote the document would
# silently discard per-agent overrides and policy text that are not its to manage.
json.dump(BUDGET, open(BPATH, "w", encoding="utf-8"), indent=2)
hb._BUDGET_CACHE["mt"] = None
check(hb.set_budget_level("thorough"), "the level can be set")
after = json.load(open(BPATH, encoding="utf-8"))
check(after["level"] == "thorough", "and it took (got %r)" % after["level"])
check(after["updated_by"] == "hud", "stamped by whoever set it")
check(after["levels"] == BUDGET["levels"], "the per-level policy text is untouched")
check(after["agent_overrides"] == BUDGET["agent_overrides"], "so are the per-agent overrides")
check(after["pm_only_key"] == BUDGET["pm_only_key"], "and so is anything else it does not know about")

check(not hb.set_budget_level("turbo"), "an unknown level is refused")
check(json.load(open(BPATH, encoding="utf-8"))["level"] == "thorough", "and changes nothing")
hb.BUDGET_PATH = J(tmp, "gone.json")
hb._BUDGET_CACHE["mt"] = None
check(not hb.set_budget_level("eco"), "a missing contract is refused rather than created")
check(hb.budget_policy("hud") == "", "and with no contract there is no policy to state")
hb.BUDGET_PATH = BPATH
print("writing: only 'level' moves; everything the pm owns survives")

hb.SHARED_DIR, hb.BUDGET_PATH = _saved
hb._ROSTER_CACHE["mt"] = None
hb._BUDGET_CACHE["mt"] = None
shutil.rmtree(tmp, ignore_errors=True)
print("\nOK - names come from folders, spend resolves per agent, and the contract is respected")
