"""Filesystem walking shared by scan, watch, index and survey. Never follows symlinks and never enters network or cloud drives."""
from __future__ import annotations

import fnmatch
import os
from pathlib import Path
from typing import Iterator

SKIP_DIRS = frozenset({"node_modules", "__pycache__", "venv", ".venv", "build", "dist", "target", "vendor", "site-packages", "snap",
                       "go", "Trash", "cmake-build-debug", "out", "Steam", "google-cloud-sdk"})
NEVER_GLOBS = ("GDrive*", "GoogleDrive*", "Google Drive*", "OneDrive*", "Dropbox*", "Nextcloud*")     # cloud sync folders: never walked or read
#   (add your own network or cloud folders with `sift dirs ignore PATTERN`)
PARTIAL_SUFFIX = (".part", ".crdownload", ".tmp", ".download", ".partial")


def walk_files(root: Path, exclude: tuple[str, ...] = (), skip_dirs: frozenset[str] = SKIP_DIRS) -> Iterator[tuple[Path, bool]]:
    """Yield (path, in_repo). Skips hidden entries, symlinks, mount points, partial downloads, excluded globs and `skip_dirs`.
    `in_repo` is True for files below a directory that contains `.git`."""
    never = NEVER_GLOBS + tuple(g for g in exclude if "/" not in g and not g.startswith("~"))
    never_paths = tuple(os.path.expanduser(g) for g in exclude if "/" in g or g.startswith("~"))
    repo_flag: dict[str, bool] = {str(root): False}
    for dirpath, dirnames, names in os.walk(root, followlinks=False):
        in_repo = repo_flag.pop(dirpath, False) or ".git" in dirnames or ".git" in names
        keep = []
        for d in sorted(dirnames):
            full = os.path.join(dirpath, d)
            if d.startswith(".") or d in skip_dirs or os.path.islink(full):
                continue
            if any(fnmatch.fnmatch(d, g) for g in never) or any(fnmatch.fnmatch(full, g.rstrip("/")) for g in never_paths) \
                    or os.path.ismount(full):
                continue
            keep.append(d)
            repo_flag[full] = in_repo
        dirnames[:] = keep
        for n in sorted(names):
            if n.startswith(".") or n.lower().endswith(PARTIAL_SUFFIX):
                continue
            p = Path(dirpath) / n
            if any(fnmatch.fnmatch(str(p), g) for g in never_paths) or any(fnmatch.fnmatch(n, g) for g in never):
                continue
            if not p.is_symlink() and p.is_file():
                yield p, in_repo
