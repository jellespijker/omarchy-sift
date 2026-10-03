import json
import os
from argparse import Namespace
from pathlib import Path

import pytest

from sift import cli, state
from sift.commands import research, review, scanning
from sift.adapters.extract import FileExtractor
from sift.api import Sift
from sift.core.model import Profile
from sift.core.tree import load_tree

from test_sift import CAPS, Fake, ROOT, TREE


@pytest.fixture
def env(tmp_path, monkeypatch, capsys):
    watch = tmp_path / "dl"; watch.mkdir()
    monkeypatch.setenv("SIFT_STATE_DIR", str(tmp_path / "state"))
    script = {"root": ("document", .95), "document": ("invoice", .5)}   # review-level at the document node
    fake = Fake(script)
    monkeypatch.setattr(scanning, "from_config", lambda cfg, tree_path=None: Sift(TREE, {"default": fake}, FileExtractor(), {"default": Profile()}))
    cfg = {"watch_dirs": [str(watch)], "endpoint": "http://127.0.0.1:1", "write_xattrs": False}
    return watch, cfg, fake, capsys


def ns(**kw):
    return Namespace(**{"all": True, "json": True, "rescan": False, "apply": False, "report": None, "once": True, "limit": 50, "tags": None, "last": None, **kw})


@pytest.mark.xattr
def test_scan_review_accept_untag_roundtrip(env):
    watch, cfg, fake, cap = env
    f = watch / "bill.txt"; f.write_text("invoice text")
    assert cli.cmd_scan(ns(dir=str(watch)), cfg) == 0
    cap.readouterr()
    cli.cmd_review(ns(), cfg)
    items = json.loads(cap.readouterr().out)
    assert [i["name"] for i in items] == ["bill.txt"] and items[0]["suggested"] == ["document/invoice"]
    assert cli.cmd_accept(ns(path=str(f)), cfg) == 0
    assert os.getxattr(f, "user.xdg.tags") == b"document-invoice"
    cap.readouterr()
    cli.cmd_review(ns(), cfg)
    assert json.loads(cap.readouterr().out) == []
    assert cli.cmd_untag(ns(), cfg) == 0
    with pytest.raises(OSError):
        os.getxattr(f, "user.xdg.tags")


def test_accepted_items_leave_the_queue_and_rescan_skips_unchanged(env):
    watch, cfg, fake, cap = env
    f = watch / "a.txt"; f.write_text("x")
    cli.cmd_scan(ns(dir=str(watch)), cfg); cli.cmd_reject(ns(path=str(f)), cfg); cap.readouterr()
    cli.cmd_review(ns(), cfg)
    assert json.loads(cap.readouterr().out) == []
    n = len(fake.seen)
    cli.cmd_scan(ns(dir=str(watch)), cfg)
    assert len(fake.seen) == n   # unchanged file not reclassified


def test_secret_file_is_never_sent_or_tagged(env):
    watch, cfg, fake, cap = env
    f = watch / "client_secret_123.json"; f.write_text('{"client_secret": "abcdefghijklmnop"}')
    cli.cmd_scan(ns(dir=str(watch)), cfg)
    assert fake.seen == []
    assert cli.cmd_accept(ns(path=str(f), tags="x"), cfg) == 1
    with pytest.raises(OSError):
        os.getxattr(f, "user.xdg.tags")


def test_rules_act_without_model(env):
    watch, cfg, fake, cap = env
    (watch / "pic.png").write_bytes(b"\x89PNG....")
    cli.cmd_scan(ns(dir=str(watch)), cfg)
    out = [json.loads(l) for l in cap.readouterr().out.splitlines() if l.startswith("{")]
    assert out[0]["outcome"] == "act" and out[0]["tags"] == ["image"] and fake.seen == []


def test_scan_apply_refused_unless_enabled(env):
    watch, cfg, fake, cap = env
    assert cli.cmd_scan(ns(dir=str(watch), apply=True), cfg) == 2


def test_partial_downloads_skipped(env):
    watch, *_ = env
    (watch / "a.crdownload").write_text("x"); (watch / "b.txt").write_text("x"); (watch / ".hidden").write_text("x")
    assert [p.name for p in scanning._files(watch)] == ["b.txt"]


def test_state_dir_private(env, tmp_path):
    assert oct(state.state_dir().stat().st_mode & 0o777) == "0o700"


def test_survey_never_walks_gdrive_or_excluded_dirs(tmp_path):
    for d in ("GDrive-Personal", "GDrive-Work/sub", "docs", "node_modules", ".hidden"):
        (tmp_path / d).mkdir(parents=True)
        (tmp_path / d / "f.txt").write_text("x")
    seen = {p.parent.name for _, p in research._survey_walk(tmp_path, 2, ())}
    assert seen == {"docs"}
    seen = {p.parent.name for _, p in research._survey_walk(tmp_path, 2, ("docs",))}
    assert seen == set()


def test_survey_sample_spreads_across_folders_and_types():
    import random
    from pathlib import Path
    strata = {f"home/{d}": [Path(f"/x/{d}/{i}{e}") for i in range(30) for e in (".py", ".pdf")] for d in "abcd"}
    strata["home/big"] = [Path(f"/x/big/{i}.txt") for i in range(5000)]
    picks = research._spread(strata, 200, random.Random(1))
    from collections import Counter
    c = Counter(s for s, _ in picks)
    assert len(picks) == 200 and c["home/big"] < 100 and all(c[f"home/{d}"] > 10 for d in "abcd")


def test_survey_counts_nested_roots_once(tmp_path, monkeypatch):
    (tmp_path / "dev" / "proj").mkdir(parents=True)
    for i in range(5):
        (tmp_path / "dev" / "proj" / f"n{i}.md").write_text("x")
    monkeypatch.setenv("SIFT_STATE_DIR", str(tmp_path / "st"))
    cli.cmd_survey(Namespace(dirs=[str(tmp_path), str(tmp_path / "dev")], n=100, depth=2, seed=1, propose=0, max_per_folder=0), {})
    rows = [json.loads(l) for l in (tmp_path / "st" / "survey_sample.jsonl").read_text().splitlines()]
    assert len(rows) == len({r["path"] for r in rows}) == 5


def test_spread_caps_files_per_folder():
    import random
    from pathlib import Path
    strata = {"big": [Path(f"/x/b{i}.txt") for i in range(1000)], "small": [Path(f"/x/s{i}.txt") for i in range(10)]}
    picks = research._spread(strata, 500, random.Random(1), max_per_folder=50)
    from collections import Counter
    assert Counter(s for s, _ in picks) == {"big": 50, "small": 10}


@pytest.mark.xattr
def test_identical_small_files_are_grouped_and_handled_together(env):
    watch, cfg, fake, cap = env
    for i in range(3):
        (watch / f"stub{i}.txt").write_text("This file cannot be downloaded.\n" * 8)
    (watch / "other.txt").write_text("a different small file " * 12)
    cli.cmd_scan(ns(dir=str(watch)), cfg); cap.readouterr()
    cli.cmd_review(ns(), cfg)
    items = json.loads(cap.readouterr().out)
    assert sorted(i["count"] for i in items) == [1, 3]
    stub = next(i for i in items if i["count"] == 3)
    assert cli.cmd_accept(ns(path=stub["path"]), cfg) == 0
    assert all(os.getxattr(watch / f"stub{i}.txt", "user.xdg.tags") for i in range(3))
    cap.readouterr(); cli.cmd_review(ns(), cfg)
    assert [i["count"] for i in json.loads(cap.readouterr().out)] == [1]


def test_a_second_scan_reports_that_one_is_running_instead_of_pretending_to_finish(env, capsys):
    import fcntl
    watch, cfg, fake, cap = env
    sd = state.state_dir()
    (sd / "index_running.json").write_text(json.dumps({"pid": os.getpid(), "started": 1, "done": 7, "budget": 200}))
    lock = open(sd / "index.lock", "w"); fcntl.flock(lock, fcntl.LOCK_EX)           # another run holds the lock
    code = cli.cmd_index(ns(budget=None, max_seconds=None, dry_run=False), cfg)
    out = capsys.readouterr()
    assert code == 4 and json.loads(out.out)["running"] is True and "7 of 200" in out.err
    cli.cmd_status(ns(), {"endpoint": "http://127.0.0.1:1"})
    assert json.loads(capsys.readouterr().out)["scan"]["done"] == 7
    (sd / "index_running.json").write_text(json.dumps({"pid": 999999999, "started": 1, "done": 1, "budget": 2}))     # a crashed run's leftovers
    assert scanning.running_scan(sd) is None


def test_untag_refuses_to_revert_everything_by_accident(env):
    from argparse import Namespace
    _, cfg, _, _ = env
    assert cli.cmd_untag(Namespace(last=None, path=None, all=False), cfg) == 2
