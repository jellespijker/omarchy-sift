from pathlib import Path
import json
from argparse import Namespace

import pytest

from sift import cli, discover as d
from sift.core.model import Answer, Capabilities

from test_sift import Fake


def test_canon_merges_order_and_plurals():
    assert d.canon("build-scripts") == d.canon("script-build") == d.canon("Build_Script")
    assert d.canon("docker-setup") != d.canon("docker-compose")


def _p(path, *tags):
    return {"path": path, "tags": list(tags)}


def test_cluster_merges_ranks_and_filters():
    props = [_p("a", "build-script", "readme"), _p("b", "script-build"), _p("c", "build-scripts", "readme"),
             _p("d", "readme"), _p("e", "rare-tag"), _p("f", "transcript"), _p("g", "transcript"), _p("h", "transcript")]
    out = d.cluster(props, existing=["transcript"], rejected=[], min_support=3)
    assert [c.tag for c in out] == ["build-script", "readme"]          # equal support sorts by name; transcript exists, rare-tag lacks support
    assert out[0].support == 3 and set(out[0].aliases) == {"script-build", "build-scripts"}
    assert [c.tag for c in d.cluster(props, rejected=["readme"], min_support=3)][0] != "readme"


class Keyword:
    """Says yes for a tag when its first word appears in the text."""
    capabilities = Capabilities(frozenset({"text"}), frozenset({"choice"}), 20, 2000, True)

    def ask(self, text, questions):
        out = {}
        for name, q in questions.items():
            yes = 0.9 if name.split("-")[0] in text else 0.05
            out[name] = Answer("yes" if yes > .5 else "no", {"yes": yes, "no": 1 - yes})
        return out


def test_evaluate_separates_specific_from_generic_tags():
    texts = {f"p{i}": f"invoice number {i} billing" for i in range(5)} | {f"q{i}": f"holiday photo notes {i}" for i in range(5)}
    cands = [d.Candidate("invoice", 5, [f"p{i}" for i in range(5)]),
             d.Candidate("notes", 5, [f"p{i}" for i in range(5)])]          # claims invoice files but fires on the others
    v = {x.tag: x for x in d.evaluate(cands, texts, Keyword())}
    assert v["invoice"].good and v["invoice"].recall == 1.0 and v["invoice"].prevalence == 0.0
    assert not v["notes"].good                                              # 'notes' only occurs in the non-members -> no recall


def test_accept_adds_to_vocabulary_and_hides_candidate(tmp_path, monkeypatch):
    monkeypatch.setenv("SIFT_STATE_DIR", str(tmp_path / "st"))
    monkeypatch.setenv("SIFT_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setattr(cli.cfgmod, "ROOT", tmp_path)
    (tmp_path / "tree.json").write_text((Path(__file__).resolve().parents[1] / "tree.json").read_text())
    (tmp_path / "st").mkdir()
    (tmp_path / "st" / "discover.json").write_text(json.dumps([{"tag": "docker", "support": 5, "recall": .8, "prevalence": .1, "good": True}]))
    ns = lambda **k: Namespace(**{"json": True, "tag": None, "desc": None, "act": None, "n": 1, "top": 5, "min_support": 3, **k})
    assert cli.cmd_discover(ns(action="accept", tag="docker"), {}) == 0
    assert "docker" in json.loads((tmp_path / "cfg" / "config.json").read_text())["vocabulary"]
    assert oct((tmp_path / "cfg" / "config.json").stat().st_mode & 0o777) == "0o600"
    import io, contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        cli.cmd_discover(ns(action="list"), {})
    assert json.loads(buf.getvalue()) == []
