import ast
import json
import os
from pathlib import Path

import pytest

from sift import config
from sift.adapters.extract import FileExtractor
from sift.adapters.jev_http import BackendError, JevHttp
from sift.adapters.stores import StoreError, XattrStore
from sift.api import Sift
from sift.core.model import Answer, Capabilities, Evidence, FileRef, Outcome, Profile
from sift.core.runner import run, sanitize_tag
from sift.core.tree import TreeError, load_tree

ROOT = Path(__file__).resolve().parents[1]
TREE = load_tree(json.loads((ROOT / "tree.json").read_text()), {"dutch": {"description": "the text is written in Dutch", "detector": "language:nl"}})
REF = FileRef("/x/a.txt", 1, 1, 1)
CAPS = Capabilities(frozenset({"text"}), frozenset({"choice", "multi"}), 20, 2000, True)


class Fake:
    """Deterministic classifier: `script` maps node id -> (label, probability)."""
    def __init__(self, script, caps=CAPS):
        self.script, self.capabilities, self.seen = script, caps, []

    def ask(self, text, questions):
        self.seen.append(text)
        out = {}
        for name, q in questions.items():
            if "yes" in getattr(q, "labels", {}):
                tag = q.instructions.split("'")[1]
                label, p = self.script.get("tag:" + tag, ("no", .99))
            else:
                label, p = self.script[name]
            labels = list(getattr(q, "labels", {}))
            rest = (1 - p) / (len(labels) - 1)
            out[name] = Answer(label, {l: (p if l == label else rest) for l in labels})
        return out


def ev(text="hello world", name="a.txt"):
    return Evidence(REF, name, "", text)


def test_confident_path_acts_and_tags():
    d = run(ev(), {"default": Fake({"root": ("document", .95), "document": ("invoice", .9)})}, TREE)
    assert d.outcome is Outcome.ACT and d.tags == ("document/invoice",)


def test_middling_confidence_goes_to_review_with_suggestion_only():
    d = run(ev(), {"default": Fake({"root": ("document", .95), "document": ("invoice", .6)})}, TREE)
    assert d.outcome is Outcome.REVIEW and d.tags == () and d.suggested == ("document/invoice",)


def test_low_confidence_at_root_is_undecided():
    d = run(ev(), {"default": Fake({"root": ("code", .3)})}, TREE)
    assert d.outcome is Outcome.UNDECIDED and d.tags == ()


def test_filename_only_never_acts():
    d = run(ev(text=None), {"default": Fake({"root": ("document", .99), "document": ("invoice", .99)})}, TREE)
    assert d.outcome is Outcome.REVIEW and d.tags == () and "filename" in d.reason


def test_uncalibrated_backend_cannot_act_without_profile():
    clf = Fake({"root": ("code", .99)}, Capabilities(frozenset({"text"}), frozenset({"choice"}), 20, 2000, False))
    assert run(ev(), {"default": clf}, TREE).outcome is Outcome.REVIEW
    assert run(ev(), {"default": clf}, TREE, {"default": Profile()}).outcome is Outcome.ACT


def test_truncates_to_backend_max_chars():
    clf = Fake({"root": ("code", .99)})
    run(ev("x" * 10_000), {"default": clf}, TREE)
    assert len(clf.seen[0]) < 2100


def test_backend_label_limit_defers_to_review():
    caps = Capabilities(frozenset({"text"}), frozenset({"choice"}), 3, 2000, True)
    assert run(ev(), {"default": Fake({}, caps)}, TREE).outcome is Outcome.REVIEW


@pytest.mark.parametrize("raw,clean", [("../../.ssh", "ssh"), ("Work/Project X", "work/project-x"), ("a/./b", "a/b"), ("", "")])
def test_sanitize_tag(raw, clean):
    assert sanitize_tag(raw) == clean


def test_tree_requires_other_label():
    with pytest.raises(TreeError):
        load_tree({"root": "r", "nodes": {"r": {"instructions": "?", "labels": {"a": None, "b": None}}}})


def _imports(path: Path, package: str) -> list[str]:
    """Absolute module names imported by a file (relative imports resolved against `package`)."""
    out: list[str] = []
    for n in ast.walk(ast.parse(path.read_text())):
        if isinstance(n, ast.Import):
            out += [a.name for a in n.names]
        elif isinstance(n, ast.ImportFrom):
            base = n.module or ""
            if n.level:
                parts = package.split(".")[: len(package.split(".")) - (n.level - 1)]
                base = ".".join([*parts, *([base] if base else [])])
            out.append(base)
            out += [f"{base}.{a.name}" for a in n.names]
    return out


def test_core_is_pure():
    """Nothing in sift.core may reach, even through other sift modules, the file system, processes, the network or the user's language
    catalogue (ADR-0001). A direct-import check alone is not enough: `from ..validate import` once hid an i18n import."""
    banned = {"os", "subprocess", "socket", "urllib", "pathlib", "shutil", "tempfile", "http", "fcntl", "sys", "sift.adapters", "sift.i18n",
              "sift.state", "sift.config", "sift.api", "sift.cli", "sift.usage", "sift.demo"}
    src = ROOT / "src"
    seen: set[str] = set()
    todo = [f"sift.core.{f.stem}" for f in (src / "sift/core").glob("*.py") if f.stem != "__init__"]
    while todo:
        mod = todo.pop()
        if mod in seen:
            continue
        seen.add(mod)
        f = src / (mod.replace(".", "/") + ".py")
        if not f.is_file():
            continue
        for imp in _imports(f, mod.rpartition(".")[0]):
            assert not any(imp == b or imp.startswith(b + ".") for b in banned), (mod, imp)
            if imp.startswith("sift."):
                todo.append(imp)


def test_core_messages_match_the_english_catalogue():
    from sift.core.validation import MESSAGES
    en = json.loads((ROOT / "src/sift/i18n/en.json").read_text())
    assert MESSAGES == {k: en[k] for k in MESSAGES}


def test_no_private_network_addresses_or_user_paths_in_shipped_files():
    """Nothing that identifies a person's network or machine may ship: no private or CGNAT addresses (apart from the documented example range),
    and no home-directory paths of a real user. Tests may use fake ones."""
    import re
    import subprocess
    out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True).stdout.split()
    files = [p for p in out if (ROOT / p).is_file()] or [str(p.relative_to(ROOT)) for p in ROOT.rglob("*") if p.is_file() and ".git" not in p.parts
                                                              and "__pycache__" not in p.parts and p.name != "config.json" and ".pytest_cache" not in p.parts]
    private = re.compile(r"\b(10\.\d{1,3}\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3}|172\.(1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}|100\.(6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d{1,3}\.\d{1,3})\b")
    homes = re.compile(r"/home/(?!u\b|username\b|a b\b|you\b|user\b)[a-z][a-z0-9_-]*/")
    for p in files:
        if p.startswith("tests/") or p.endswith((".png", ".jpg")):
            continue
        text = (ROOT / p).read_text(errors="ignore")
        text = text.replace("192.168.1.0/24", "")                       # the documented example range
        assert not private.search(text), (p, private.search(text).group(0))
        assert not homes.search(text), (p, homes.search(text).group(0))


# --- config trust
def test_endpoint_trust():
    config.check_endpoint("http://127.0.0.1:8791", [], False)
    config.check_endpoint("http://localhost:8791", [], False)
    config.check_endpoint("https://example.com", [], False)
    config.check_endpoint("http://100.64.1.2:8791", ["100.64.0.0/10"], False)
    for bad in ("http://8.8.8.8:8791", "http://100.64.1.2:8791", "ftp://x"):
        with pytest.raises(config.ConfigError):
            config.check_endpoint(bad, [], False)
    config.check_endpoint("http://8.8.8.8:8791", [], True)


# --- jev response validation
Q = {"root": __import__("sift.core.model", fromlist=["Choice"]).Choice("?", {"a": None, "b": None})}


@pytest.mark.parametrize("data", [
    {"answers": {}}, {"answers": {"root": {"type": "choice", "choice": "c", "probabilities": {"a": .5, "b": .5}}}},
    {"answers": {"root": {"type": "choice", "choice": "a", "probabilities": {"a": .9, "b": .5}}}},
    {"answers": {"root": {"type": "choice", "choice": "a", "probabilities": {"a": 1.2, "b": -.2}}}}])
def test_jev_rejects_bad_responses(data):
    with pytest.raises(BackendError):
        JevHttp._parse(data, Q)


def test_jev_accepts_valid_response():
    a = JevHttp._parse({"answers": {"root": {"type": "choice", "choice": "a", "probabilities": {"a": .7, "b": .3}}}}, Q)
    assert a["root"].choice == "a"


def test_jev_empty_text_is_not_sent():
    with pytest.raises(BackendError):
        JevHttp("http://127.0.0.1:1").ask("  ", Q)


# --- extractor + xattr store safety
def test_extractor_text_and_symlink(tmp_path):
    f = tmp_path / "a.txt"; f.write_text("hello")
    assert FileExtractor().extract(f).text == "hello"
    l = tmp_path / "l.txt"; l.symlink_to(f)
    assert FileExtractor().extract(l).text is None  # O_NOFOLLOW
    (tmp_path / "e.txt").write_text("")
    assert FileExtractor().extract(tmp_path / "e.txt").text is None


@pytest.mark.xattr
def test_xattr_store_guards(tmp_path):
    root = tmp_path / "root"; root.mkdir()
    f = root / "a.txt"; f.write_text("x")
    other = tmp_path / "outside.txt"; other.write_text("x")
    store = XattrStore([root])
    ex = FileExtractor()
    from dataclasses import replace
    d = lambda p: replace(Sift(TREE, {"default": Fake({"root": ("code", .99)})}, ex).classify(p), outcome=Outcome.ACT, tags=("code",))
    with pytest.raises(StoreError):                      # outside roots
        store.apply(d(other), ["code"])
    link = root / "ln.txt"; link.symlink_to(other)
    with pytest.raises(StoreError):                      # symlink out of roots
        store.apply(d(link), ["code"])
    dec = d(f)
    f.write_text("changed content")                      # file changed since classification
    with pytest.raises(StoreError):
        store.apply(dec, ["code"])
    dec = d(f)
    assert store.apply(dec, ["code"]) == "tagged"
    assert os.getxattr(f, "user.xdg.tags") == b"code"


def test_jev_retries_then_reports_unreachable():
    c = JevHttp("http://127.0.0.1:1", timeout=1, retries=1, backoff=0)
    with pytest.raises(BackendError, match="unreachable"):
        c.ask("hello", Q)


def _vocab_script(**tags):
    return {"root": ("code", .95), **{f"tag:{k}": v for k, v in tags.items()}}


def test_vocabulary_tag_acts_when_confident_and_is_suggested_when_middling():
    clf = Fake(_vocab_script(transcript=("yes", .9), contract=("yes", .6)))
    d = run(ev("uh ehm contract agreement clause " * 20), {"default": clf}, TREE)
    assert d.outcome is Outcome.ACT
    assert "transcript" in d.tags and "code" in d.tags
    assert d.suggested == ("contract",) and "contract" not in d.tags
    assert d.scores["transcript"] == pytest.approx(.9)


def test_long_text_is_scored_in_several_chunks_and_best_chunk_wins():
    from sift.core.runner import chunks
    text = "a" * 10_000
    parts = chunks(text, 1500)
    assert 2 <= len(parts) <= 5 and all(len(p) <= 1500 for p in parts)
    assert chunks("short", 1500) == ["short"]


def test_uninformative_other_path_is_not_written_as_a_tag():
    d = run(ev(), {"default": Fake({"root": ("other", .95)})}, TREE)
    assert "other" not in d.tags


def test_vocabulary_needs_text():
    clf = Fake(_vocab_script(transcript=("yes", .99)))
    d = run(ev(text=None), {"default": clf}, TREE)
    assert "transcript" not in d.tags and not d.scores


def test_bad_user_vocabulary_is_skipped_and_reported_not_fatal():
    t = load_tree({"root": "r", "nodes": {"r": {"instructions": "?", "labels": {"a": None, "other": None}}}},
                  {"no-desc": {}, "bad-regex": {"description": "x", "require": "(unclosed"}, "evil": {"description": "x", "require": "(a+)+$"},
                   "bad-act": {"description": "x", "act": 7}, "fine": {"description": "ok"}})
    assert list(t.vocabulary) == ["fine"] and len(t.problems) == 4



def test_confident_data_or_log_skips_vocabulary_and_tiny_text_is_not_scored():
    clf = Fake({"root": ("log", .95), "tag:transcript": ("yes", .99)})
    d = run(ev("x" * 500), {"default": clf}, TREE)
    assert "transcript" not in d.scores and "transcript" not in d.tags       # general tags are skipped for logs
    d = run(ev("tiny"), {"default": Fake(_vocab_script(transcript=("yes", .99)))}, TREE)
    assert d.scores == {}


@pytest.mark.xattr
def test_xattr_tags_are_flattened_for_dolphin(tmp_path):
    from dataclasses import replace
    root = tmp_path / "r"; root.mkdir(); f = root / "a.txt"; f.write_text("x")
    dec = replace(Sift(TREE, {"default": Fake({"root": ("code", .99)})}, FileExtractor()).classify(f), outcome=Outcome.ACT)
    XattrStore([root]).apply(dec, ["document/invoice", "transcript"])
    assert os.getxattr(f, "user.xdg.tags") == b"document-invoice,transcript"


def test_dutch_is_decided_by_the_detector_not_the_model():
    nl = "Dat is een goed idee en ik denk dat we het met de hele groep kunnen doen. " * 6
    en = "This is a good idea and I think that we can do it with the whole group of people. " * 6
    clf = Fake({"root": ("code", .95), "tag:dutch": ("yes", .99)})        # a confident model must not matter
    assert "dutch" in run(ev(nl), {"default": clf}, TREE).tags
    assert "dutch" not in run(ev(en), {"default": clf}, TREE).tags
    assert not any("dutch" in q for q in clf.seen)                         # never even asked


def test_evidence_gate_drops_tags_without_keywords():
    clf = Fake(_vocab_script(**{"3d-printing": ("yes", .99)}))
    assert "3d-printing" not in run(ev("a long text about holidays and gardens " * 20), {"default": clf}, TREE).tags
    assert "3d-printing" in run(ev("filament and nozzle settings for the printer " * 20), {"default": clf}, TREE).tags


def test_language_detector_cases():
    from sift.core.lang import detect_language
    assert detect_language("Dit is een tekst over de planning van het project en wat we nog moeten doen om het af te maken. " * 3) == "nl"
    assert detect_language("cmake_minimum_required(VERSION 3.20) project(demo) add_executable(app main.cpp) target_link_libraries(app PRIVATE fmt)") in (None, "en", "mixed")
    assert detect_language("tiny") is None


def test_build_files_named_txt_are_not_prose():
    from sift.indexer import eligible
    assert not eligible(".txt", True, frozenset(), "CMakeLists.txt")
    assert not eligible(".txt", False, frozenset(), "requirements.txt")
    assert eligible(".txt", True, frozenset(), "notes.txt")
