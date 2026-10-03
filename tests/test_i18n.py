import json
import re
from pathlib import Path

import pytest

from sift import i18n

DIR = Path(__file__).resolve().parents[1] / "src" / "sift" / "i18n"
EN = json.loads((DIR / "en.json").read_text(encoding="utf-8"))
PH = re.compile(r"\{(\w+)\}")
CODE = re.compile(r"`[^`]*`")
LANGS = [l for l in i18n.SUPPORTED if l != "en"]
BIDI = re.compile("[‎‏‪-‮⁦-⁩]")


def test_language_detection_follows_the_system():
    assert i18n.detect({"LANG": "nl_NL.UTF-8"}) == "nl"
    assert i18n.detect({"LC_ALL": "de_DE.UTF-8", "LANG": "en_US.UTF-8"}) == "de"                    # LC_ALL wins
    assert i18n.detect({"LANGUAGE": "xx:fr:en", "LANG": "en_US.UTF-8"}) == "fr"                     # first supported entry of the list
    assert i18n.detect({"LANG": "zh_TW.UTF-8"}) == "zh" and i18n.detect({"LANG": "ar_EG.UTF-8"}) == "ar"
    assert i18n.detect({"LANG": "C"}) == "en" and i18n.detect({}) == "en" and i18n.detect({"LANG": "ja_JP.UTF-8"}) == "en"


def test_user_choice_overrides_the_system():
    assert i18n.resolve({"language": "es"}, {"LANG": "nl_NL.UTF-8"}) == ("es", "es")
    assert i18n.resolve({"language": "auto"}, {"LANG": "nl_NL.UTF-8"}) == ("auto", "nl")
    assert i18n.resolve({"language": "klingon"}, {"LANG": "fr_FR"}) == ("auto", "fr")
    assert i18n.resolve({}, {"SIFT_LANG": "de", "LANG": "en_US"}) == ("de", "de")
    assert i18n.direction("ar") == "rtl" and i18n.direction("nl") == "ltr"


@pytest.mark.parametrize("lang", LANGS)
def test_catalog_is_complete_and_safe(lang):
    path = DIR / f"{lang}.json"
    assert path.exists(), f"{lang}.json is missing"
    cat = json.loads(path.read_text(encoding="utf-8"))
    assert set(cat) == set(EN), (sorted(set(EN) - set(cat))[:5], sorted(set(cat) - set(EN))[:5])
    for key, text in cat.items():
        assert isinstance(text, str) and text.strip(), key
        assert sorted(PH.findall(text)) == sorted(PH.findall(EN[key])), f"{lang}:{key} placeholders differ: {text!r}"
        assert CODE.findall(text) == CODE.findall(EN[key]), f"{lang}:{key} code spans must stay verbatim: {text!r}"
        assert not BIDI.search(text), f"{lang}:{key} contains invisible direction controls"
        assert "\n" not in text or "\n" in EN[key], key


@pytest.mark.parametrize("lang", LANGS)
def test_short_labels_stay_short(lang):
    cat = json.loads((DIR / f"{lang}.json").read_text(encoding="utf-8"))
    for key in ("tab.review", "tab.audit", "tab.tags", "tab.settings", "common.cancel", "common.save", "common.add", "confirm.confirm", "undo.button",
                "audit.right", "audit.wrong", "audit.preview", "demo.exit", "sched.daily", "sched.off", "set.apiKey"):
        limit = 6 if lang == "zh" else 16
        assert len(cat[key]) <= limit, f"{lang}:{key} is {len(cat[key])} characters: {cat[key]!r}"


@pytest.mark.parametrize("lang", LANGS)
def test_translated_messages_reach_the_terminal(lang, monkeypatch):
    monkeypatch.setenv("SIFT_LANG", lang)
    i18n._current = None
    i18n.init({"language": lang})
    from sift.validate import ValidationError, valid_tag
    with pytest.raises(ValidationError) as e:
        valid_tag("!!!")
    assert str(e.value) != EN["cli.val.tagNeedsLetter"]                                              # translated, not English
    i18n._current = None


def test_unknown_key_is_visible_not_fatal():
    i18n.init({"language": "en"})
    assert i18n.t("no.such.key") == "no.such.key" and i18n.t("hero.files", n=3) == "Files: 3"
