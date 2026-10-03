"""On-disk state under $XDG_STATE_HOME/sift (mode 0700): report, review decisions, undo log, watcher memory.
Paths and labels only; never file content.

This module owns every rule about how state is stored, so no other module parses JSONL or writes a file its own way:
tolerant reads (a torn or foreign line is skipped, never fatal), locked single-write appends, atomic whole-file writes, advisory
locks, the report schema version, and compaction so the files cannot grow forever."""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Iterator

REPORT_SCHEMA = 2                       # format written now
REPORT_READABLE = frozenset({2})        # formats this release can use; a newer format never forces a re-scan of older entries


def state_dir() -> Path:
    from . import demo
    if demo.active():
        base = demo.paths()["state"]
        base.mkdir(parents=True, exist_ok=True, mode=0o700)
        return base
    base = Path(os.environ.get("SIFT_STATE_DIR") or Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "sift")
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(base, 0o700)
    return base


def _lockfile(path: Path) -> Path:
    return path.with_name(path.name + ".flock")


@contextlib.contextmanager
def lock(path: Path, blocking: bool = True) -> Iterator[bool]:
    """Advisory lock on a file (released when the process dies). Yields False instead of waiting when `blocking` is False and it is taken."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    got = False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
            got = True
        except OSError:
            got = False
        yield got
    finally:
        if got:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def append(path: Path, obj: dict) -> None:
    """One line, one write call on an O_APPEND descriptor, under the file's lock so a compaction cannot swallow it."""
    line = (json.dumps({**obj, "ts": round(time.time(), 3)}) + "\n").encode()
    with lock(_lockfile(path)):
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, line)
        finally:
            os.close(fd)


REQUIRED = {"report.jsonl": ("path",), "reviews.jsonl": ("path",), "verdicts.jsonl": ("path", "tag", "verdict"),
            "undo.jsonl": ("path", "inode", "old", "new"), "merges.jsonl": ("path", "src", "dst"), "usage.jsonl": ("b",)}


def read(path: Path) -> list[dict]:
    """Every readable record. Invalid bytes, torn lines, lines that are not objects and records missing the keys their file always has
    are skipped: state must never stop a command, whatever a crash, an older release or a stray edit left in it."""
    need = REQUIRED.get(path.name, ())
    try:
        raw = path.read_bytes()
    except OSError:
        return []
    out: list[dict] = []
    for line in raw.splitlines():
        try:
            obj = json.loads(line.decode("utf-8", errors="replace"))
        except ValueError:
            continue
        if isinstance(obj, dict) and all(k in obj for k in need):
            out.append(obj)
    return out


def write_atomic(path: Path, data: str | bytes, mode: int = 0o600, durable: bool = True) -> None:
    """Replace a file in one step (unique temp name, fsync, rename): readers see the old or the new content, never a half file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data.encode() if isinstance(data, str) else data)
            f.flush()
            if durable:
                os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def write_json(path: Path, obj: Any, mode: int = 0o600) -> None:
    write_atomic(path, json.dumps(obj), mode)


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_bytes().decode("utf-8", errors="replace"))
    except (OSError, ValueError):
        return default


def usable(e: dict) -> bool:
    """A report entry this release understands and can match to a file."""
    return e.get("v") in REPORT_READABLE and bool(e.get("ref"))


def is_current(e: dict | None, ref: list[int]) -> bool:
    """Has this file already been classified and not changed since?"""
    return bool(e) and usable(e) and e["ref"] == ref


MAINTAIN_AT = {"report.jsonl": 5_000_000, "undo.jsonl": 3_000_000, "verdicts.jsonl": 1_000_000, "reviews.jsonl": 1_000_000,
               "merges.jsonl": 5_000_000, "usage.jsonl": 2_000_000}
UNDO_KEEP = 20000


def maintain(sd: Path) -> dict[str, tuple[int, int]]:
    """Keep state files from growing without bound: only the newest record per file (report, reviews), per file and tag (verdicts,
    merges) is kept, undo history is capped and old usage is rolled up by day. Runs after each index pass, and only when a file is big."""
    done: dict[str, tuple[int, int]] = {}

    def big(name: str) -> bool:
        try:
            return (sd / name).stat().st_size > MAINTAIN_AT[name]
        except OSError:
            return False
    try:
        if big("report.jsonl"):
            done["report"] = compact_latest(sd / "report.jsonl")
        if big("reviews.jsonl"):
            done["reviews"] = compact_latest(sd / "reviews.jsonl")
        if big("verdicts.jsonl"):
            done["verdicts"] = compact_latest(sd / "verdicts.jsonl", ("path", "tag"))
        if big("merges.jsonl"):
            done["merges"] = compact_latest(sd / "merges.jsonl", ("path", "src"))
        if big("undo.jsonl"):
            done["undo"] = keep_tail(sd / "undo.jsonl", UNDO_KEEP)
        if big("usage.jsonl"):
            from . import usage
            done["usage"] = usage.roll_up(sd / "usage.jsonl")
    except OSError:
        pass                                    # maintenance is best effort; the next run tries again
    return done


def _rewrite(path: Path, rows: list[dict]) -> None:
    write_atomic(path, "".join(json.dumps(r) + "\n" for r in rows))


def compact_latest(path: Path, keys: tuple[str, ...] = ("path",)) -> tuple[int, int]:
    """Keep only the newest record per key (default: per path). Returns (before, after). Appends wait on the file lock meanwhile."""
    with lock(_lockfile(path)):
        rows = read(path)
        latest: dict[Any, dict] = {}
        for r in rows:
            k = tuple(r.get(x) for x in keys)
            if None not in k:
                latest[k] = r
        if len(latest) < len(rows):
            _rewrite(path, list(latest.values()))
        return len(rows), len(latest)


def keep_tail(path: Path, n: int) -> tuple[int, int]:
    """Keep the newest n records (for logs where only recent history matters, such as undo)."""
    with lock(_lockfile(path)):
        rows = read(path)
        if len(rows) > n:
            _rewrite(path, rows[-n:])
        return len(rows), min(len(rows), n)


def latest_by_path(entries: list[dict]) -> dict[str, dict]:
    return {e["path"]: e for e in entries if "path" in e}


def pending_review(report: list[dict], reviews: list[dict]) -> list[dict]:
    """Latest report entry per path that needs a human and has no newer accept/reject."""
    rep, rev = latest_by_path(report), latest_by_path(reviews)
    out = []
    for p, e in rep.items():
        if e.get("outcome") != "review" and not e.get("suggested"):
            continue
        r = rev.get(p)
        if r and r["ts"] >= e["ts"]:
            continue
        out.append(e)
    return sorted(out, key=lambda e: -(e["steps"][-1][2] if e.get("steps") else 0))


def digest_of(path: str, size: int, max_group_size: int = 8192) -> str:
    """Hash of the bytes of a small file, '' for large or unreadable ones. Used only to group identical files."""
    if size > max_group_size:
        return ""
    import hashlib
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            import re
            text = os.read(fd, max_group_size + 1).decode("utf-8", errors="replace")
            # normalise ids and numbers so near-identical stubs (same error page, different request id) group together
            text = re.sub(r"[0-9a-fA-F]{8}-[0-9a-fA-F-]{8,}|\d+", "#", text)
            return hashlib.sha1(text.encode()).hexdigest()
        finally:
            os.close(fd)
    except OSError:
        return ""


def group_identical(entries: list[dict]) -> list[tuple[dict, list[dict]]]:
    """Group review entries whose (small) files are byte-identical. Returns (representative, members) pairs, order kept."""
    groups: dict[str, list[dict]] = {}
    order: list[str] = []
    for e in entries:
        d = digest_of(e["path"], e["ref"][2]) if e.get("ref") else ""
        key = d or "path:" + e["path"]
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(e)
    return [(groups[k][0], groups[k]) for k in order]
