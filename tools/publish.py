"""Commit specific files and push them to GitHub (the live site reads from there).

Only the paths given are staged, so nothing else in the working tree is ever published.
A lock file keeps the image agent, the review page and push-badge.sh from colliding.
"""
import fcntl, os, subprocess

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCK = os.path.join(ROOT, "state", ".publish.lock")


def _git(*args):
    return subprocess.run(["git", "-C", ROOT, *args], capture_output=True, text=True, timeout=120)


def commit_and_push(paths, message, log=print):
    rel = [os.path.relpath(p, ROOT) for p in paths]
    with open(LOCK, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        r = _git("add", "--", *rel)
        if r.returncode:
            log(f"git add failed: {r.stderr.strip()}")
            return False
        if not _git("diff", "--cached", "--quiet", "--", *rel).returncode:
            return True  # nothing changed
        r = _git("commit", "-m", message, "--only", "--", *rel)
        if r.returncode:
            log(f"git commit failed: {r.stderr.strip()}")
            return False
        for _ in range(2):
            r = _git("push", "-q", "origin", "HEAD:main")
            if not r.returncode:
                return True
            _git("pull", "--rebase", "--autostash", "-q", "origin", "main")
        log(f"git push failed: {r.stderr.strip()}")
        return False
