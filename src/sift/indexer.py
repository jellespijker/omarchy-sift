"""Selecting what the background indexer should classify next. Filesystem reads only; no model calls."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable, Mapping

from . import state
from .walk import walk_files

PROSE = {".md", ".txt", ".rst", ".pdf", ".docx", ".odt", ".pptx", ".odp", ".xlsx", ".ods", ".tex", ".eml"}
TEXTISH = PROSE | {".csv", ".json", ".log", ".xml", ".html", ".yaml", ".yml", ".ini", ".conf", ".toml", ".py", ".c", ".cpp", ".h", ".hpp",
                   ".js", ".ts", ".sh", ".qml", ".sql", ".lua", ".gcode", ".patch", ".diff"}


CODE_NAMES = frozenset({"cmakelists.txt", "requirements.txt", "constraints.txt", "conanfile.txt", "cmakecache.txt", "robots.txt",
                        "license.txt", "licence.txt", "notice.txt", "authors.txt", "version.txt", "manifest.txt", "sources.txt"})


def eligible(ext: str, in_repo: bool, rule_exts: frozenset[str], name: str = "") -> bool:
    """Inside git repositories only prose is classified (source files are not tagged). Elsewhere text-like files and rule types are.
    Build and metadata files that happen to be .txt (CMakeLists.txt, requirements.txt, ...) are code, never prose."""
    if name.lower() in CODE_NAMES:
        return False
    if in_repo:
        return ext in PROSE
    return ext in TEXTISH or ext in rule_exts


def candidates(roots: Iterable[Path], exclude: tuple[str, ...], known: Mapping[str, dict], rule_exts: frozenset[str],
               schema: int = 0) -> list[tuple[int, Path]]:
    """(mtime_ns, path) of eligible files that have no up-to-date report entry, newest first."""
    out: list[tuple[int, Path]] = []
    for root in roots:
        for p, in_repo in walk_files(root, exclude):
            if not eligible(p.suffix.lower(), in_repo, rule_exts, p.name):
                continue
            try:
                st = os.stat(p, follow_symlinks=False)
            except OSError:
                continue
            old = known.get(str(p))
            if state.is_current(old, [st.st_ino, st.st_mtime_ns, st.st_size]):
                continue
            out.append((st.st_mtime_ns, p))
    out.sort(reverse=True)
    return out
