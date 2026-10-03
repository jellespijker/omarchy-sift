import os
import time

import pytest

from sift.adapters.llm_chat import ChatProposer
from sift.adapters.stores import read_tags, write_tags
from sift.core.runner import sanitize_tag, tag_gate
from sift.core.tree import TagDef
from sift.discover import canon
from sift.validate import (ValidationError, clean_text, normalize_tag, valid_glob, valid_pattern, valid_tag, valid_unit_interval)

from test_backends import chat_reply, serve  # noqa: F401


@pytest.mark.parametrize("raw,expected", [
    ("税务", "税务"), ("فاتورة", "فاتورة"), ("Ñandú Café", "ñandú-café"), ("My Tag", "my-tag"),
    ("a,b", "a-b"), ("a/b", "a-b"), ("x‮txt", "xtxt"), ("hindi: हिन्दी", "hindi-हिन्दी"), ("  ", ""), ("!!!", ""),
])
def test_tag_names_keep_any_script_and_lose_dangerous_characters(raw, expected):
    assert normalize_tag(raw) == expected
    assert "," not in normalize_tag(raw) and "/" not in normalize_tag(raw)


def test_valid_tag_limits():
    assert valid_tag("税务") == "税务"
    for bad in ("", "!!!", "x" * 41):
        with pytest.raises(ValidationError):
            valid_tag(bad)


def test_runner_sanitize_keeps_unicode_and_path_structure():
    assert sanitize_tag("文档/发票") == "文档/发票" and sanitize_tag("../../.ssh") == "ssh" and sanitize_tag("a/./b") == "a/b"


def test_description_cleaning():
    assert clean_text("  tax\n documents\t and returns ") == "tax documents and returns"
    assert clean_text("rtl‮ override\x00 and nul") == "rtl override and nul"
    assert clean_text("税务文件和报表") == "税务文件和报表"
    for bad in ("", "   \n", "x" * 301, 5):
        with pytest.raises(ValidationError):
            clean_text(bad)


@pytest.mark.parametrize("pattern", ["(unclosed", "(a+)+$", "(.*)*x", "(\\w+\\s?)+$", "", "x" * 201])
def test_dangerous_or_invalid_patterns_are_refused(pattern):
    with pytest.raises(ValidationError):
        valid_pattern(pattern)


def test_good_patterns_pass_and_gate_survives_a_bad_one():
    assert valid_pattern(r"\b(invoice|factuur)\b")
    assert tag_gate(TagDef("x", require="(unclosed"), "text") is False          # hand-edited garbage: no crash, no tag
    t = time.time()
    tag_gate(TagDef("x", require=r"\b(invoice|factuur)\b"), "a" * 30000)
    assert time.time() - t < 1


def test_globs_and_thresholds():
    assert valid_glob("~/Documents/private/*") and valid_glob("*.bak")
    for bad in ("", "x" * 201, "\x00"):
        with pytest.raises(ValidationError):
            valid_glob(bad)
    assert valid_unit_interval("0.5") == 0.5
    for bad in (-1, 1.5, "abc", None):
        with pytest.raises(ValidationError):
            valid_unit_interval(bad)


def test_discovery_merges_non_latin_names():
    assert canon("税务") == canon("税务") != frozenset()
    assert canon("فاتورة-ضريبة") == canon("ضريبة-فاتورة")
    assert canon("build-scripts") == canon("script-build")


@pytest.mark.xattr
def test_foreign_encoded_tags_survive_a_rewrite(tmp_path):
    f = tmp_path / "a.txt"; f.write_text("x")
    os.setxattr(f, "user.xdg.tags", b"caf\xe9,ok")                          # latin-1 bytes written by some other tool
    from sift.adapters.stores import XattrStore, replace_tags
    from sift.core.model import Decision, FileRef, Outcome
    st = os.stat(f)
    store = XattrStore([tmp_path])
    replace_tags(store, Decision(FileRef(str(f), st.st_ino, st.st_mtime_ns, st.st_size), (), Outcome.ACT), {"ok": "fine"})
    assert os.getxattr(f, "user.xdg.tags") == b"caf\xe9,fine"                  # untouched bytes preserved exactly
    write_tags(f, read_tags(f) + ["税务"])
    assert os.getxattr(f, "user.xdg.tags").endswith("税务".encode())


def test_chat_proposer_accepts_names_in_any_script(serve):  # noqa: F811
    s = serve(chat_reply('{"tags": ["税务文件", "فاتورة", "Tax Return", "x"]}'))
    assert ChatProposer(s.url, "m").propose("f", ["t"], [], 5) == ["税务文件", "فاتورة", "tax-return"]
