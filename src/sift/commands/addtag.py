"""`sift add-tag PATH TAG [--description TEXT]`: the user's own tag for a file, from review. Existing or new.

The tag is written to the file, the review item is resolved, and the choice is recorded as a verdict so the tag can improve: verdicts feed
`sift audit`, `sift eval-tags` and `sift tune`. A new tag with a description joins the vocabulary, so future scans look for it."""
from __future__ import annotations

import json
import sys
from pathlib import Path

from .. import config as cfgmod
from .. import state
from ..adapters.stores import StoreError, XattrStore
from ..api import load_tree_for
from ..adapters.extract import stat_ref
from ..core.model import Decision, Outcome
from ..validate import ValidationError, clean_text, valid_tag


def add_tag(cfg: dict, sd: Path, path: str, tag: str, description: str | None) -> dict:
    tag = valid_tag(tag)
    if description is not None and description.strip():
        description = clean_text(description, what="description")
    else:
        description = None
    real = Path(path).expanduser().resolve(strict=True)
    from ..adapters.extract import FileExtractor
    ev = FileExtractor().extract(real)
    if ev.metadata.get("sensitive"):
        raise StoreError("the file is flagged as a possible secret; sensitive files are never tagged")
    tree = load_tree_for(cfg)
    entry = state.latest_by_path(state.read(sd / "report.jsonl")).get(str(real)) or {}
    suggested = tag in set(entry.get("suggested", []))
    store = XattrStore(cfgmod.dirs(cfg), sd / "undo.jsonl")
    ref = stat_ref(real)
    store.apply(Decision(ref, (), Outcome.ACT, tags=(tag,)), [tag])
    new = tag not in tree.vocabulary
    if new and description:
        from .tags import add_vocabulary
        add_vocabulary(tag, description)
    state.append(sd / "verdicts.jsonl", {"path": str(real), "tag": tag, "verdict": "ok" if suggested else "missed",
                                         "ref": [ref.inode, ref.mtime_ns, ref.size], "source": "review"})
    state.append(sd / "reviews.jsonl", {"path": str(real), "action": "accept", "tags": [tag], "manual": True})
    return {"tag": tag, "new": new, "learned": (not new) or bool(description), "suggested": suggested}


def run(a, cfg) -> int:
    try:
        r = add_tag(cfg, state.state_dir(), a.path, a.tag, a.description)
    except (ValidationError, StoreError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    if a.json:
        print(json.dumps(r))
    else:
        print(f"tagged with {r['tag']}" + ("" if r["learned"] else " (no description: Sift will not look for it in other files; add one in the Tags tab)"))
    return 0


def register(sub) -> None:
    p = sub.add_parser("add-tag", help="tag a file with your own tag, existing or new, and learn from it")
    p.add_argument("path"); p.add_argument("tag")
    p.add_argument("--description", help="what the tag means; with it a new tag joins your vocabulary so other files can get it")
    p.add_argument("--json", action="store_true")
    p.set_defaults(run=run)
