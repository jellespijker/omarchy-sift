"""TagStore adapters: a report-only store (default) and an xattr store."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Sequence

from .. import state
from ..i18n import t
from ..core.model import Decision

XATTR = "user.xdg.tags"


def read_tags(path) -> list[str]:
    """Tags on a file. Bytes that are not valid UTF-8 (written by other tools) are kept losslessly via surrogateescape."""
    try:
        raw = os.getxattr(path, XATTR, follow_symlinks=False)
    except OSError:
        return []
    return [t for t in raw.decode("utf-8", "surrogateescape").split(",") if t]


def write_tags(path, tags: list[str]) -> None:
    if tags:
        os.setxattr(path, XATTR, ",".join(tags).encode("utf-8", "surrogateescape"), follow_symlinks=False)
    else:
        try:
            os.removexattr(path, XATTR, follow_symlinks=False)
        except OSError:
            pass


class StoreError(RuntimeError):
    pass


def xattr_supported(d: Path) -> bool:
    """Can tags be stored on files in this folder? (Some filesystems, such as certain network and container mounts, refuse user xattrs.)"""
    import tempfile
    try:
        with tempfile.TemporaryFile(dir=d) as f:                       # no directory entry is ever created
            os.setxattr(f.fileno(), "user.sift_probe", b"1")
        return True
    except OSError:
        return False


def _open_private(path: Path, mode: str = "a"):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | (os.O_APPEND if mode == "a" else os.O_TRUNC), 0o600)
    return os.fdopen(fd, "w")


class ReportStore:
    """Appends one JSON line per decision. Paths and labels only, never file content."""

    def __init__(self, report: Path):
        self.report = report

    def apply(self, decision: Decision, tags: Sequence[str]) -> str:
        line = {"path": decision.ref.path, "ref": [decision.ref.inode, decision.ref.mtime_ns, decision.ref.size],
                "outcome": decision.outcome.value, "tags": list(tags),
                "suggested": list(decision.suggested), "reason": decision.reason, "v": state.REPORT_SCHEMA,
                "scores": {k: round(v, 3) for k, v in decision.scores.items() if v >= 0.2},
                "steps": [[s.node, s.label, round(s.probability, 4), s.basis] for s in decision.steps]}
        state.append(self.report, line)
        return "reported"


class XattrStore:
    def __init__(self, roots: Sequence[Path], undo_log: Path | None = None):
        self.roots = [Path(r).expanduser().resolve() for r in roots]
        self.undo_log = undo_log
        self._lockfile = undo_log.parent / "xattr.flock" if undo_log else None

    def guard(self):
        """Serialise read-modify-write of a file's tags between Sift processes (timer, panel, terminal)."""
        import contextlib
        return state.lock(self._lockfile) if self._lockfile else contextlib.nullcontext(True)

    def _check(self, decision: Decision) -> Path:
        p = Path(decision.ref.path)
        real = p.resolve(strict=True)
        if real != p.absolute() and p.is_symlink():
            raise StoreError(t("cli.store.symlink"))
        if not any(real == r or r in real.parents for r in self.roots):
            raise StoreError(t("cli.store.outsideRoots"))
        st = os.stat(real, follow_symlinks=False)
        if (st.st_ino, st.st_mtime_ns, st.st_size) != (decision.ref.inode, decision.ref.mtime_ns, decision.ref.size):
            raise StoreError(t("cli.store.changed"))
        return real

    def apply(self, decision: Decision, tags: Sequence[str]) -> str:
        real = self._check(decision)
        with self.guard():
            old = read_tags(real)
            # Dolphin's tags:/ view treats "/" as a path separator, so hierarchical path tags are flattened for xattrs
            merged = sorted(set(old) | {t.replace("/", "-") for t in tags})
            new = ",".join(merged)
            if self.undo_log:                    # logged first: a crash can then be undone, a failed write leaves an entry undo skips
                st = os.stat(real, follow_symlinks=False)
                state.append(self.undo_log, {"path": str(real), "inode": st.st_ino, "old": ",".join(old), "new": new})
            try:
                write_tags(real, merged)
            except OSError as e:
                raise StoreError(f"cannot write tags here: {e.strerror or e}") from e
        return "tagged"


def remove_tags(store: "XattrStore", decision: Decision, tags: Sequence[str]) -> str:
    """Remove specific tags (flattened like written ones) from a file, keeping any others. Logged for undo."""
    real = store._check(decision)
    drop = {t.replace("/", "-") for t in tags}
    with store.guard():
        old = read_tags(real)
        if not old:
            return "none"
        keep = [t for t in old if t not in drop]
        if keep == old:
            return "unchanged"
        if store.undo_log:
            st = os.stat(real, follow_symlinks=False)
            state.append(store.undo_log, {"path": str(real), "inode": st.st_ino, "old": ",".join(old), "new": ",".join(keep)})
        write_tags(real, keep)
    return "pruned"


def replace_tags(store: "XattrStore", decision: Decision, mapping: dict[str, str]) -> str:
    """Rename tags on a file (`old -> new`; new == "" drops the tag). Names are compared in their flattened on-disk form."""
    real = store._check(decision)
    m = {k.replace("/", "-"): v.replace("/", "-") for k, v in mapping.items()}
    with store.guard():
        old = read_tags(real)
        if not old:
            return "none"
        new = sorted({m.get(t, t) for t in old} - {""})
        if new == sorted(old):
            return "unchanged"
        if store.undo_log:
            st = os.stat(real, follow_symlinks=False)
            state.append(store.undo_log, {"path": str(real), "inode": st.st_ino, "old": ",".join(old), "new": ",".join(new)})
        write_tags(real, new)
    return "renamed"


def undo(undo_log: Path, roots: Sequence[Path], last: int | None = None, path: str | None = None) -> tuple[int, int]:
    """Restore previous xattr values, newest first. Skips files that changed identity or whose tags were edited since.
    With `path`, only that file's changes are considered, so one file can be put back without touching anything else."""
    rs = [Path(r).expanduser().resolve() for r in roots]
    done = skipped = 0
    entries = state.read(undo_log)
    if path:
        real = str(Path(path).expanduser().resolve())
        entries = [e for e in entries if e["path"] == real]
    for e in reversed(entries[-last:] if last else entries):
        p = Path(e["path"])
        try:
            real = p.resolve(strict=True)
            if not any(real == r or r in real.parents for r in rs) or os.stat(real, follow_symlinks=False).st_ino != e["inode"]:
                raise StoreError("identity")
            cur = ",".join(read_tags(real))
            if cur != e["new"]:
                raise StoreError("edited since")
            write_tags(real, [t for t in e["old"].split(",") if t])
            done += 1
        except (StoreError, OSError):
            skipped += 1
    return done, skipped
