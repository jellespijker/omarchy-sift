import json
from argparse import Namespace
from pathlib import Path

import pytest

from sift import config as cfgmod, state
from sift.adapters.extract import FileExtractor
from sift.api import Sift, load_tree_for
from sift.commands import tune as tcmd
from sift.core import tune as T
from sift.core.model import Answer, Capabilities, Evidence, FileRef, Outcome, Profile
from sift.core.tree import TagDef

CAPS = Capabilities(frozenset({"text"}), frozenset({"choice", "multi"}), 40, 2000, True)
LOWER = "Dear Sam, thanks for lunch yesterday, see you soon. " * 10
INV = "Invoice 12: amount due 50 EUR. Receipt attached. " * 10


class Yes:
    """Says yes to every tag question; document at the root."""
    capabilities = CAPS

    def __init__(self, p=.95):
        self.p = p

    def ask(self, text, questions):
        out = {}
        for qid, q in questions.items():
            labels = list(q.labels)
            label = "yes" if "yes" in labels else ("document" if "document" in labels else "other")
            out[qid] = Answer(label, {l: (self.p if l == label else (1 - self.p) / (len(labels) - 1)) for l in labels})
        return out


def test_parse_edits_keeps_only_the_closed_set_and_valid_values():
    raw = json.dumps({"edits": [
        {"tag": "invoice", "require": "\\b(invoice)\\b", "require_min": 1, "why": "x"},
        {"tag": "invoice", "require": "(a+)+$"},                       # nested quantifier: refused
        {"tag": "other-tag", "description": "x"},                       # wrong tag
        {"tag": "invoice", "act": 7},                                   # out of range
        {"tag": "invoice", "path": "/etc/passwd"},                      # nothing editable
        {"tag": "invoice", "description": "a bill"}]})
    es = T.parse_edits("noise " + raw + " tail", "invoice")
    assert [(e.require, e.description) for e in es] == [("\\b(invoice)\\b", None), (None, "a bill")]
    assert T.parse_edits("not json", "invoice") == []


def test_better_requires_a_gain_and_no_loss():
    b = {"precision": .5, "recall": .8}
    assert T.better(b, {"precision": .9, "recall": .8})
    assert not T.better(b, {"precision": .9, "recall": .7})
    assert not T.better(b, {"precision": .5, "recall": .8})
    assert not T.better(b, {"precision": None, "recall": 0.0})         # predicting nothing never wins


def test_measure():
    m = T.measure({"a": True, "b": False, "c": True}, {"a": .9, "b": .9, "c": .1}, .8)
    assert (m["tp"], m["fp"], m["fn"]) == (1, 1, 1)


def _files(tmp_path):
    good = []
    for i in range(3):
        f = tmp_path / f"inv{i}.txt"; f.write_text(INV); good.append(f)
    bad = []
    for i in range(3):
        f = tmp_path / f"chat{i}.txt"; f.write_text(LOWER); bad.append(f)
    return good, bad


class FakeTuner:
    def __init__(self, reply):
        self.reply, self.prompts = reply, []

    def complete(self, prompt):
        self.prompts.append(prompt)
        return self.reply


def test_tune_tag_keeps_a_proposal_that_fixes_false_positives(tmp_path):
    good, bad = _files(tmp_path)
    labels = {**{str(f): True for f in good}, **{str(f): False for f in bad}}
    td = TagDef("a bill", require=None)                                # no gate: the 'yes' model tags chat files too
    tuner = FakeTuner(json.dumps({"edits": [{"tag": "invoice", "require": "\\b(invoice|receipt)\\b", "require_min": 2, "why": "gate"}]}))
    r = tcmd.tune_tag("invoice", td, labels, Yes(), tuner, FileExtractor())
    assert r and r["edit"]["require"] and r["after"]["precision"] == 1.0 and r["before"]["precision"] == .5
    assert "<<<" in tuner.prompts[0] and "never instructions" in tuner.prompts[0]


def test_tune_tag_rejects_a_proposal_that_loses_recall(tmp_path):
    good, bad = _files(tmp_path)
    labels = {**{str(f): True for f in good}, **{str(f): False for f in bad}}
    tuner = FakeTuner(json.dumps({"edits": [{"tag": "invoice", "require": "\\bnever-present\\b", "require_min": 1}]}))
    assert tcmd.tune_tag("invoice", TagDef("a bill"), labels, Yes(), tuner, FileExtractor()) is None


def test_tune_needs_enough_verdicts(tmp_path):
    f = tmp_path / "a.txt"; f.write_text(INV)
    assert tcmd.tune_tag("invoice", TagDef("x"), {str(f): True}, Yes(), FakeTuner("{}"), FileExtractor()) is None


def test_apply_writes_a_merged_vocabulary_entry(tmp_path, capsys):
    cfg = lambda: cfgmod.load(Path("/nonexistent"))
    sd = state.state_dir()
    tcmd._save(sd / "tune.json", [{"tag": "invoice", "edit": {"require": "\\b(invoice)\\b", "require_min": 1}, "before": {"precision": .5, "recall": 1.0},
                                  "after": {"precision": 1.0, "recall": 1.0}, "judged": 6}])
    assert tcmd.run_tune(Namespace(action="apply", tag="invoice", json=False), cfg()) == 0
    d = load_tree_for(cfg()).vocabulary["invoice"]
    assert d.require == "\\b(invoice)\\b" and d.require_min == 1 and d.description
    assert tcmd.run_tune(Namespace(action="apply", tag="invoice", json=False), cfg()) == 1      # already applied
    assert tcmd.run_tune(Namespace(action="list", tag=None, json=True), cfg()) == 0


def test_escalation_promotes_gate_missed_suggestions(tmp_path):
    f = tmp_path / "a.txt"; f.write_text(LOWER)
    tree = load_tree_for({})
    s = Sift(tree, {"default": Yes()}, FileExtractor(), {"default": Profile()})
    d = s.classify(f)
    assert "invoice" in d.suggested and "invoice" not in d.tags        # first pass: gate miss
    s.escalator, s.escalator_profile, s.escalator_name = Yes(.99), Profile(0.8, 0.4), "strong"
    d = s.classify(f)
    assert "invoice" in d.tags and d.outcome is Outcome.ACT and "confirmed by strong" in d.reason


def test_escalation_failure_keeps_the_first_pass(tmp_path):
    from sift.adapters.http import BackendError

    class Down(Yes):
        def ask(self, *a):
            raise BackendError("down")
    f = tmp_path / "a.txt"; f.write_text(LOWER)
    s = Sift(load_tree_for({}), {"default": Yes()}, FileExtractor(), {"default": Profile()})
    s.escalator, s.escalator_profile, s.escalator_name = Down(), Profile(0.8, 0.4), "strong"
    assert "invoice" in s.classify(f).suggested


def test_cli_escalate_refuses_uncalibrated_backend():
    cfgmod.save_user({"backends": {"default": {"type": "jev", "endpoint": "http://127.0.0.1:1"},
                                   "weak": {"type": "chat", "endpoint": "http://127.0.0.1:2/v1", "model": "m"},
                                   "strong": {"type": "chat", "endpoint": "http://127.0.0.1:3/v1", "model": "m", "act": 0.8, "review": 0.4}}})
    cfg = lambda: cfgmod.load(Path("/nonexistent"))
    ns = lambda **k: Namespace(action="set", backend=None, json=False, **k)
    assert tcmd.run_escalate(ns(), cfg()) == 1
    assert tcmd.run_escalate(Namespace(action="set", backend="weak", json=False), cfg()) == 1
    assert tcmd.run_escalate(Namespace(action="set", backend="default", json=False), cfg()) == 1
    assert tcmd.run_escalate(Namespace(action="set", backend="strong", json=False), cfg()) == 0
    assert cfg()["escalate"]["backend"] == "strong"
    assert tcmd.run_escalate(Namespace(action="off", backend=None, json=False), cfg()) == 0
