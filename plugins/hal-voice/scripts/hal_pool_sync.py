#!/usr/bin/env python3
"""
Cross-device pool sync over git.

Pushes lines this machine synthesized and pulls lines other devices made, into the
shared pool repo (cfg.pool_repo).  Only manifest.json can ever conflict - mp3 names are
deterministic (hal_pool_NN / hal_auto_<hash>), so two devices never write different
bytes to the same filename - and conflicts are resolved by taking the UNION of entries.

Run:  python plugins/hal-voice/scripts/hal_pool_sync.py
(any python; needs git available)
"""
import json, os, shutil, subprocess, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hal_common as hc


def find_git():
    g = shutil.which("git")
    if g:
        return g
    for c in (r"C:\Program Files\Git\cmd\git.exe", r"C:\Program Files\Git\bin\git.exe"):
        if os.path.exists(c):
            return c
    return None


def git(repo, *args, check=False):
    r = subprocess.run([GIT, "-C", repo, *args], capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed:\n{r.stderr.strip()}")
    return r


def union_entries(*lists):
    """Merge manifest entry lists: base hal_pool_NN lines (numeric order) first, then
    auto lines, de-duplicated by file then by text."""
    seen_file, seen_text, base, auto = set(), set(), [], []
    for lst in lists:
        for e in (lst or []):
            f = e.get("file", ""); t = e.get("text", "").strip().lower()
            if not f or f in seen_file or (t and t in seen_text):
                continue
            seen_file.add(f); seen_text.add(t)
            (base if f.startswith("hal_pool_") else auto).append(e)
    base.sort(key=lambda e: e.get("file", ""))
    return base + auto


def read_json_blob(repo, ref_path):
    r = git(repo, "show", ref_path)
    if r.returncode != 0:
        return []
    try:
        return json.loads(r.stdout)
    except Exception:
        return []


def main():
    global GIT
    GIT = find_git()
    cfg = hc.load_config()
    repo, pdir = cfg.get("pool_repo"), hc.pool_dir(cfg)
    if not GIT:
        print("git not found on this machine - cannot sync."); return
    if not (repo and os.path.isdir(os.path.join(repo, ".git"))):
        print(f"pool_repo is not a git repo: {repo!r}. Run hal_setup.py from your clone."); return

    pool_rel = os.path.relpath(pdir, repo).replace("\\", "/")
    man_rel  = pool_rel + "/manifest.json"

    # 1. stage + commit any local lines (force: the pool dir may match a gitignore glob)
    git(repo, "add", "-f", "--", pool_rel)
    if git(repo, "diff", "--cached", "--quiet").returncode != 0:
        n = len(hc.load_pool(pdir))
        git(repo, "commit", "-m", f"hal pool: sync local lines ({n} total)")
        print("committed local pool changes")
    else:
        print("no new local lines to commit")

    # 2. pull; resolve a manifest conflict by union
    pull = git(repo, "pull", "--no-rebase", "--no-edit")
    if pull.returncode != 0:
        if "CONFLICT" in (pull.stdout + pull.stderr) and "manifest.json" in (pull.stdout + pull.stderr):
            ours   = read_json_blob(repo, f":2:{man_rel}")
            theirs = read_json_blob(repo, f":3:{man_rel}")
            merged = union_entries(ours, theirs)
            with open(os.path.join(repo, man_rel), "w", encoding="utf-8") as f:
                json.dump(merged, f, indent=2)
            git(repo, "add", "--", man_rel)
            if git(repo, "commit", "--no-edit").returncode == 0:
                print(f"resolved manifest conflict by union ({len(merged)} lines)")
            else:
                print("could not complete merge commit; resolve manually:", repo); return
        else:
            print("pull failed (offline? no upstream?):\n" + (pull.stderr.strip() or pull.stdout.strip()))
            print("local pool still works; re-run sync later."); return
    else:
        print("pulled remote pool")

    # 3. push
    push = git(repo, "push")
    if push.returncode == 0:
        print("pushed.")
    else:
        print("push failed (no write access / no upstream?):\n" + (push.stderr.strip() or push.stdout.strip()))

    print(f"pool now has {len(hc.load_pool(pdir))} lines at {pdir}")


if __name__ == "__main__":
    main()
