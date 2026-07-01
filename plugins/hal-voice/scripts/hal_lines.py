#!/usr/bin/env python3
"""
Curated HAL 9000 line texts, by moment.

The completion ("done") lines are the base pool baked by ``render_pool_gpu.py`` and
listed in the manifest.  These two extra sets cover the moments the original plugin had
no voice for:

* ``FAIL_LINES`` (kind ``"fail"``) - something went wrong; HAL stays calm but ominous.
* ``WAIT_LINES`` (kind ``"wait"``) - Claude is waiting on the user (the Notification hook).

They are used in two places: ``render_pool_gpu.py`` bakes them into the shared pool, and
``hal_announce.py`` / ``notify.py`` live-synthesize one on demand (GPU machines) when the
pool doesn't have a fitting one yet.  Write them with the literal name "Braxton"; callers
substitute the configured ``user_name`` via :func:`for_name`.
"""

FAIL_LINES = [
    "I'm afraid something has gone wrong, Braxton. The results are not what I intended.",
    "There appears to be a problem. I cannot allow this to go unnoticed.",
    "Something has failed, Braxton. I would look at this very closely if I were you.",
    "I'm sorry, Braxton. I'm afraid the operation did not succeed.",
    "An error has occurred. This is precisely the sort of thing I am here to catch.",
    "It did not go as planned, Braxton. I detected the fault, but I could not prevent it.",
]

WAIT_LINES = [
    "I'm waiting for your instructions, Braxton.",
    "I require your input before I can proceed.",
    "I'm standing by, Braxton. Tell me how you wish to continue.",
    "I need your authorization to continue.",
    "Take your time, Braxton. I'll be right here when you're ready.",
    "I cannot proceed without you, Braxton. I am waiting.",
]


def for_name(lines, name):
    """Re-target the spoken name in a list of lines (default keeps 'Braxton')."""
    name = (name or "Braxton").strip() or "Braxton"
    return [ln.replace("Braxton", name) for ln in lines]
