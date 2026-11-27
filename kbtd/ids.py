"""The default card-ID prefix for a board that has no counter line yet.

The prefix is the first three letters of the GitHub repository's name (from
the ``origin`` remote), else of the git repository's folder, else of the
board file's folder: ``thehumanworks/kbtd`` gives ``KBT``. It is derived only
when a board gets its first ID; after that the counter line in the file holds
it. See docs/adr/0004-card-ids.md.
"""

import os
import re
import subprocess
from pathlib import Path

FALLBACK = "TSK"
_GITHUB = re.compile(r"(?:[a-z+]+://)?(?:[^@/]+@)?github\.com[:/][^/]+/([^/]+?)(?:\.git)?/?")


def _git(folder: Path, *args: str) -> str:
    # GIT_DIR from a hook or a watcher action would point git at another repository.
    env = {k: v for k, v in os.environ.items() if k not in ("GIT_DIR", "GIT_WORK_TREE")}
    try:
        done = subprocess.run(["git", "-C", str(folder), *args], capture_output=True, text=True,
                              env=env, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return ""
    return done.stdout.strip() if done.returncode == 0 else ""


def github_repo(url: str) -> str:
    """The repository name in a github.com remote URL, or ""."""
    m = _GITHUB.fullmatch(url.strip())
    return m[1] if m else ""


def from_name(name: str) -> str:
    """The first three letters of ``name``, in capitals."""
    return re.sub(r"[^A-Za-z]", "", name)[:3].upper() or FALLBACK


def default_prefix(board: Path) -> str:
    folder = board.parent
    root = _git(folder, "rev-parse", "--show-toplevel")
    name = github_repo(_git(folder, "remote", "get-url", "origin")) if root else ""
    return from_name(name or (Path(root).name if root else folder.name))
