import json
from argparse import Namespace
from pathlib import Path

import pytest

from sift import config as cfgmod
from sift.api import Sift, load_tree_for
from sift.commands import profiles as pcmd
from sift.core import profiles as prof
from sift.core import prompts as P
from sift.core.model import Answer, Capabilities, Evidence, FileRef, Outcome, Profile
from sift.core.runner import run
from sift.core.tree import load_tree
from sift.validate import ValidationError, valid_prompt

CAPS = Capabilities(frozenset({"text"}), frozenset({"choice", "multi"}), 40, 2000, True)
REF = FileRef("/x/a.txt", 1, 1, 1)
TEXT = "some long enough text " * 20
DATA = json.loads((Path(__file__).parent.parent / "tree.json").read_text())


class Recorder:
    capabilities = CAPS

    def __init__(self):
        self.questions = []

    def ask(self, text, questions):
        out = {}
        for qid, q in questions.items():
            self.questions.append(q.instructions)
            labels = list(q.labels)
            label = "yes" if "yes" in labels else ("document" if "document" in labels else labels[0])
            out[qid] = Answer(label, {l: (.95 if l == label else .05 / (len(labels) - 1)) for l in labels})
        return out


def tree(**kw):
    return load_tree(DATA, **kw)


def test_prompt_validation_limits_placeholders_and_controls():
    assert valid_prompt("Is {tag} here?\nYes.", ("tag",)) == "Is {tag} here?\nYes."
    for bad in ("", "   ", "x" * 2000, "uses {secret}", "{description}"):
        with pytest.raises(ValidationError):
            valid_prompt(bad, ("tag",))
    assert "\x07" not in valid_prompt("a\x07b‮c", ())


def test_render_replaces_only_named_placeholders():
    assert P.render("{tag}: {description} {other} {0}", tag="a", description="b") == "a: b {other} {0}"


def test_custom_tag_question_reaches_the_classifier():
    clf = Recorder()
    run(Evidence(REF, "a.txt", "", TEXT), {"default": clf}, tree(prompts={"tag_question": "ZZZ {tag} :: {description}"}), {"default": Profile()})
    assert any(q.startswith("ZZZ transcript ::") for q in clf.questions)


def test_default_tag_question_is_unchanged():
    clf = Recorder()
    run(Evidence(REF, "a.txt", "", TEXT), {"default": clf}, tree(), {"default": Profile()})
    assert any(q.startswith("Does this text have the tag 'transcript'?") for q in clf.questions)


def _p(**kw):
    return prof.parse_profile({"name": "p", **kw})


def test_profile_needs_a_match_and_validates():
    with pytest.raises(ValidationError):
        _p()
    with pytest.raises(ValidationError):
        _p(match={"extensions": [".pdf"]}, act=3)
    with pytest.raises(ValidationError):
        _p(match={"extensions": [".pdf"]}, prompts={"tag_question": "{nope}"})
    assert _p(match={"extensions": ["PDF"]}).extensions == frozenset({".pdf"})


def test_matching_is_and_and_most_specific_wins():
    a = _p(match={"extensions": [".pdf"]}); a = prof.Profile("a", extensions=a.extensions)
    b = prof.Profile("b", dirs=("/home/u/tax",), extensions=frozenset({".pdf"}))
    c = prof.Profile("c", dirs=("/home/u",))
    ps = (a, c, b)
    assert prof.select(ps, "/home/u/tax/x.pdf").name == "b"
    assert prof.select(ps, "/home/u/other/x.pdf").name == "c" or prof.select(ps, "/home/u/other/x.pdf").name == "a"
    assert prof.select(ps, "/home/u/other/x.pdf").name == "c"           # longer dir beats extension-only
    assert prof.select(ps, "/srv/x.pdf").name == "a"
    assert prof.select(ps, "/srv/x.txt") is None
    assert prof.select(ps, "/home/username/x.txt") is None             # prefix must end at a path boundary
    g = prof.Profile("g", globs=("*Invoice*",))
    assert prof.select((g,), "/a/my-INVOICE-1.txt").name == "g"


def test_derive_restricts_tags_adds_vocabulary_and_overrides_thresholds():
    base = tree()
    p = prof.parse_profile({"name": "x", "match": {"extensions": [".txt"]}, "tags": ["invoice"], "act": 0.95,
                            "vocabulary": {"tax-return": "a filed tax return"}, "exclude_labels": ["code"],
                            "prompts": {"tag_question": "Q {tag}"}, "node_prompts": {}})
    d = prof.derive(base, p)
    assert set(d.vocabulary) == {"invoice", "tax-return"}
    assert d.vocabulary["invoice"].act == 0.95
    assert "code" not in d.nodes["root"].labels and "other" in d.nodes["root"].labels
    assert "code" in base.nodes["root"].labels and "transcript" in base.vocabulary      # base tree untouched
    assert d.prompts["tag_question"] == "Q {tag}"


def test_no_tags_profile_scores_nothing():
    clf = Recorder()
    d = prof.derive(tree(), _p(match={"extensions": [".txt"]}, tags=[]))
    run(Evidence(REF, "a.txt", "", TEXT), {"default": clf}, d, {"default": Profile()})
    assert not any(q.startswith("Does this text have the tag") for q in clf.questions)


def test_sift_selects_profile_by_path_and_skips(tmp_path):
    f = tmp_path / "private" / "a.txt"
    f.parent.mkdir()
    f.write_text(TEXT)
    cfg = {"profiles": [{"name": "private", "match": {"dirs": [str(f.parent)]}, "skip": True}]}
    t = load_tree_for(cfg)
    clf = Recorder()
    from sift.adapters.extract import FileExtractor
    s = Sift(t, {"default": clf}, FileExtractor(), {"default": Profile()})
    d = s.classify(f)
    assert d.outcome is Outcome.UNDECIDED and "private" in d.reason and not clf.questions
    other = tmp_path / "b.txt"; other.write_text(TEXT)
    s.classify(other)
    assert clf.questions


def test_packaged_pdf_profile_removes_code_label():
    t = load_tree_for({})
    p = prof.select(t.profiles, "/x/y.pdf")
    assert p and p.name == "pdf" and "code" not in prof.derive(t, p).nodes["root"].labels


def test_user_profile_overrides_packaged_by_name_and_bad_ones_are_reported():
    t = load_tree_for({"profiles": [{"name": "pdf", "match": {"extensions": [".pdf"]}, "tags": []}, {"name": "bad"}, "junk"]})
    assert [p.name for p in t.profiles].count("pdf") == 1 and prof.select(t.profiles, "/a.pdf").tags == ()
    probs = [x for x in t.problems if x.startswith("profile")]
    assert any("bad" in x for x in probs) and len(probs) == 2 and len(t.problems) == 2


def test_bad_global_prompt_is_ignored_and_reported():
    t = load_tree_for({"prompts": {"tag_question": "{nope}", "propose": "x {n}", "unknown": "y"}})
    assert "tag_question" not in t.prompts and len(t.problems) == 2


def test_node_prompt_changes_the_root_question():
    clf = Recorder()
    t = load_tree_for({"prompts": {"nodes": {"root": "Is this a CUSTOMROOT?"}}})
    run(Evidence(REF, "a.txt", "", TEXT), {"default": clf}, t, {"default": Profile()})
    assert "Is this a CUSTOMROOT?" in clf.questions


def test_chat_adapters_use_templates_and_keep_the_safety_text():
    from sift.adapters.llm_chat import ChatLLM, ChatProposer, SAFETY
    c = ChatLLM("http://127.0.0.1:1/v1", "m", system="Be terse.")
    assert c.system.startswith("Be terse.") and SAFETY in c.system
    assert SAFETY in ChatLLM("http://127.0.0.1:1/v1", "m").system


def _ns(**kw):
    base = dict(json=False, name=None, text=None, dir=None, ext=None, glob=None, skip=False, tags=None, no_tags=False, act=None,
                exclude_label=None, prompt=None, skip_vocab=None)
    return Namespace(**{**base, **kw})


def test_cli_prompts_set_reset_and_refuse_bad_input(capsys):
    cfg = lambda: cfgmod.load(Path("/nonexistent"))
    assert pcmd.run_prompts(_ns(action="set", name="tag_question", text="{bad}"), cfg()) == 1
    assert pcmd.run_prompts(_ns(action="set", name="nope", text="x"), cfg()) == 1
    assert pcmd.run_prompts(_ns(action="set", name="tag_question", text="About '{tag}'? {description}"), cfg()) == 0
    assert load_tree_for(cfg()).prompts["tag_question"].startswith("About")
    assert pcmd.run_prompts(_ns(action="set", name="node:root", text="Which kind?"), cfg()) == 0
    assert load_tree_for(cfg()).nodes["root"].instructions == "Which kind?"
    assert pcmd.run_prompts(_ns(action="reset", name="tag_question"), cfg()) == 0
    assert pcmd.run_prompts(_ns(action="reset", name="node:root"), cfg()) == 0
    assert not load_tree_for(cfg()).prompts and load_tree_for(cfg()).nodes["root"].instructions.startswith("What kind")


def test_cli_profiles_add_test_remove(tmp_path, capsys):
    cfg = lambda: cfgmod.load(Path("/nonexistent"))
    d = tmp_path / "Tax"
    assert pcmd.run_profiles(_ns(action="add", name="tax", dir=[str(d)], tags="invoice", act=0.9), cfg()) == 0
    assert pcmd.run_profiles(_ns(action="add", name="oops"), cfg()) == 1                       # no match given
    assert pcmd.run_profiles(_ns(action="add", name="oops", ext=[".x"], act=2), cfg()) == 1
    capsys.readouterr()
    assert pcmd.run_profiles(_ns(action="test", name=str(d / "a.pdf"), json=True), cfg()) == 0
    assert json.loads(capsys.readouterr().out)["profile"] == "tax"
    assert pcmd.run_profiles(_ns(action="remove", name="pdf"), cfg()) == 1                      # packaged: not removable
    assert pcmd.run_profiles(_ns(action="remove", name="tax"), cfg()) == 0
    assert all(p.name != "tax" for p in load_tree_for(cfg()).profiles)


# ---- label-specific tags, gate as suggestion, skip_vocabulary ---------------------------------------------------------

PY = "import os\nfrom sys import argv\n\ndef main():\n    return 1\n\nclass A:\n    pass\n" * 4
LOG = "Traceback (most recent call last):\n  File x\nSegmentation fault (core dumped)\nfatal error: exception\n" * 3


class Scripted(Recorder):
    def __init__(self, root, yes):
        super().__init__()
        self.root, self.yes = root, set(yes)

    def ask(self, text, questions):
        out = {}
        for qid, q in questions.items():
            labels = list(q.labels)
            if "yes" in labels:
                self.questions.append(qid)
                label = "yes" if qid in self.yes else "no"
            else:
                label = self.root if self.root in labels else "other"
            out[qid] = Answer(label, {l: (.95 if l == label else .05 / (len(labels) - 1)) for l in labels})
        return out


def test_label_specific_tags_apply_only_to_their_label():
    t = tree()
    clf = Scripted("code", ["python", "invoice"])
    d = run(Evidence(REF, "a.py", "", PY), {"default": clf}, t, {"default": Profile()})
    assert "python" in d.tags and "python" in clf.questions and "crash-log" not in clf.questions
    clf = Scripted("document", ["python"])
    run(Evidence(REF, "a.txt", "", PY), {"default": clf}, t, {"default": Profile()})
    assert "python" not in clf.questions


def test_logs_get_log_tags_but_not_general_tags():
    clf = Scripted("log", ["crash-log", "invoice"])
    d = run(Evidence(REF, "a.log", "", LOG), {"default": clf}, tree(), {"default": Profile()})
    assert "crash-log" in d.tags and "invoice" not in clf.questions


def test_gate_miss_becomes_a_suggestion_not_a_tag():
    clf = Scripted("document", ["invoice"])
    d = run(Evidence(REF, "a.txt", "", "Dear Sam, thanks for lunch yesterday, see you soon. " * 10), {"default": clf}, tree(), {"default": Profile()})
    assert "invoice" not in d.tags and "invoice" in d.suggested and "keyword evidence" in d.reason and d.outcome is Outcome.REVIEW


def test_gate_hit_still_acts():
    clf = Scripted("document", ["invoice"])
    d = run(Evidence(REF, "a.txt", "", "Invoice 12: amount due 50 EUR. Receipt attached. " * 10), {"default": clf}, tree(), {"default": Profile()})
    assert "invoice" in d.tags


def test_profile_can_tag_logs_with_general_tags():
    clf = Scripted("log", ["invoice"])
    p = prof.parse_profile({"name": "x", "match": {"extensions": [".log"]}, "skip_vocabulary": []})
    run(Evidence(REF, "a.log", "", "Invoice amount due receipt " * 20), {"default": clf}, prof.derive(tree(), p), {"default": Profile()})
    assert "invoice" in clf.questions
