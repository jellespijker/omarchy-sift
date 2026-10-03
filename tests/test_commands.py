import json
import os
from argparse import Namespace
from pathlib import Path

import pytest

from sift import config as cfgmod, state
from sift.api import load_tree_for
from sift.commands import dirs as dirs_cmd, doctor, schedule, setup, tags as tagcmd
from sift.core.model import Answer, Capabilities

XATTR = "user.xdg.tags"


def ns(**kw):
    base = dict(json=False, force=False, value=None, tag=None, tags=[], into=None, desc=None, act=None, require=None, require_min=None,
                dry_run=False, no_systemctl=True, discover=None, interval=None, yes=True, preset=None, app=None, endpoint=None, model=None, path=None,
                api_key_env=None, api_key_file=None, api_key_keyring=False, auth_header=None, auth_scheme=None, stdin=False, name=None, consent=False, trust=False, dir=None, key=None)
    return Namespace(**{**base, **kw})


# ---- directories ------------------------------------------------------------------------------------------------------

def _cfg(tmp_path):
    return cfgmod.load(tmp_path / "no-defaults")          # reads only the isolated user config, like the real CLI does


def test_dirs_add_remove_ignore_roundtrip(tmp_path, capsys):
    d = tmp_path / "docs"; d.mkdir()
    assert dirs_cmd.run(ns(action="add", value=str(d)), _cfg(tmp_path)) == 0
    assert str(d) in cfgmod.user_value("dirs")
    assert dirs_cmd.run(ns(action="add", value=str(tmp_path / "missing")), _cfg(tmp_path)) == 1
    assert dirs_cmd.run(ns(action="add", value="/"), _cfg(tmp_path)) == 1
    dirs_cmd.run(ns(action="ignore", value="*.bak"), _cfg(tmp_path))
    dirs_cmd.run(ns(action="ignore", value="~/private/*"), _cfg(tmp_path))
    assert cfgmod.user_value("ignore") == ["*.bak", "~/private/*"]
    dirs_cmd.run(ns(action="unignore", value="*.bak"), _cfg(tmp_path))
    assert cfgmod.user_value("ignore") == ["~/private/*"]
    dirs_cmd.run(ns(action="remove", value=str(d)), _cfg(tmp_path))
    assert str(d) not in cfgmod.user_value("dirs")


def test_ignore_patterns_apply_to_names_and_paths(tmp_path):
    from sift.walk import walk_files
    for d in ("keep", "skipme", "private"):
        (tmp_path / d).mkdir(); (tmp_path / d / "a.txt").write_text("x"); (tmp_path / d / "b.bak").write_text("x")
    names = {str(p.relative_to(tmp_path)) for p, _ in walk_files(tmp_path, ("skipme", "*.bak", str(tmp_path / "private") + "*"))}
    assert names == {"keep/a.txt"}


# ---- schedule ----------------------------------------------------------------------------------------------------------

def test_parse_interval():
    assert schedule.parse_interval("30m") == 1800 and schedule.parse_interval("daily") == 86400 and schedule.parse_interval("2h") == 7200
    assert schedule.parse_interval("off") is None and schedule.parse_interval("3d") == 259200
    for bad in ("1m", "soon", "10"):
        with pytest.raises(cfgmod.ConfigError):
            schedule.parse_interval(bad)


def test_schedule_writes_and_removes_units(tmp_path):
    schedule.install("45m", "weekly", systemctl=False)
    sd = schedule.systemd_dir()
    assert "OnUnitInactiveSec=2700s" in (sd / "sift-index.timer").read_text()
    assert f'ExecStart="{schedule.bin_path()}" index' in (sd / "sift-index.service").read_text()
    assert (sd / "sift-discover.timer").exists() and cfgmod.user_value("schedule") == {"index": "45m", "discover": "weekly"}
    schedule.install("off", "off", systemctl=False)
    assert not list(sd.glob("sift-*"))


# ---- setup ------------------------------------------------------------------------------------------------------------

def test_build_backend_presets():
    t = setup.build_backend("typesafe", {"api_key_env": "TS_KEY", "consent": True})
    assert t == {"type": "jev", "endpoint": "https://api.typesafe.ai", "model": "jev-latest", "api_key_env": "TS_KEY", "consent": True}
    o = setup.build_backend("ollama", {"model": "gemma4:e2b"})
    assert o["type"] == "chat" and o["endpoint"].endswith("/v1") and "act" not in o          # suggestions only by default
    assert setup.build_backend("ollama", {"model": "m", "trust": True})["act"] == 0.9
    with pytest.raises(cfgmod.ConfigError):
        setup.build_backend("nope", {})


def test_setup_refuses_remote_without_consent_and_never_stores_keys(monkeypatch, tmp_path):
    monkeypatch.setenv("TS_KEY", "sk-very-secret")
    assert setup.run_setup(ns(preset="typesafe", api_key_env="TS_KEY"), {}) == 1             # no consent: nothing saved
    assert not cfgmod.user_config_path().exists()
    assert setup.run_setup(ns(preset="typesafe", api_key_env="TS_KEY", consent=True, force=True), {}) == 0   # force: network not reachable here
    saved = cfgmod.user_config_path().read_text()
    assert "sk-very-secret" not in saved and '"api_key_env": "TS_KEY"' in saved


def test_config_set_validates_keys(capsys):
    assert setup.run_config(ns(action="set", key="write_xattrs", value="true"), {}) == 0
    assert cfgmod.user_value("write_xattrs") is True
    assert setup.run_config(ns(action="set", key="threshold_act", value="abc"), {}) == 2
    assert setup.run_config(ns(action="set", key="backends", value="x"), {}) == 2


def test_show_redacts_secret_fields(capsys):
    setup.run_config(ns(action="show"), {"backends": {"default": {"type": "jev", "endpoint": "http://x", "api_key": "leak", "api_key_env": "E"}}})
    out = capsys.readouterr().out
    assert "leak" not in out and "<hidden>" in out


# ---- doctor ------------------------------------------------------------------------------------------------------------

def test_doctor_flags_missing_pieces(tmp_path):
    rows = doctor.checks({"dirs": [str(tmp_path / "nope")]})
    levels = {n: l for l, n, _ in rows}
    assert levels["classifier"] == "fail" and levels[f"folder {tmp_path / 'nope'}"] == "fail"


# ---- tag manager -------------------------------------------------------------------------------------------------------

@pytest.fixture
def tagged(tmp_path, xattr_required):
    """Three files carrying tags on disk and in the report."""
    root = tmp_path / "files"; root.mkdir()
    sd = state.state_dir()
    cfgmod.save_user({"vocabulary": {"build-script": {"description": "build scripts"}, "script-build": {"description": "scripts for builds"}}})
    cfg = {"dirs": [str(root)]}
    files = {}
    for name, tags in (("a.txt", ["build-script", "code"]), ("b.txt", ["script-build"]), ("c.txt", ["data"])):
        p = root / name; p.write_text(name)
        os.setxattr(p, XATTR, ",".join(tags).encode())
        st = os.stat(p)
        state.append(sd / "report.jsonl", {"path": str(p), "ref": [st.st_ino, st.st_mtime_ns, st.st_size], "outcome": "act", "tags": tags, "suggested": [], "v": 2})
        files[name] = p
    return cfg, sd, files


def trun(a, cfg):
    """One CLI invocation: every call re-reads the user config, like a fresh process would."""
    return tagcmd.run(a, {**cfg, **cfgmod.load(Path("/nonexistent"))})


def cfg_now(cfg):
    return {**cfg, **cfgmod.load(Path("/nonexistent"))}


def tags_of(p):
    return sorted(os.getxattr(p, XATTR).decode().split(",")) if XATTR in os.listxattr(p) else []


def test_inventory_counts_tags_and_kinds(tagged):
    cfg, sd, _ = tagged
    rows = {r["tag"]: r for r in tagcmd.inventory(cfg_now(cfg), sd)}
    assert rows["build-script"]["files"] == 1 and rows["build-script"]["kind"] == "vocabulary"
    assert rows["data"]["kind"] == "builtin" and rows["image"]["kind"] == "rule" and rows["transcript"]["kind"] == "vocabulary"


def test_similar_groups_find_reordered_and_plural_names():
    g = tagcmd.similar_groups({"build-script": 5, "script-build": 2, "build-scripts": 1, "docker": 9, "docker-compose": 3, "research": 4, "reseach": 1})
    merged = {frozenset([x["keep"], *x["merge"]]) for x in g}
    assert frozenset({"build-script", "script-build", "build-scripts"}) in merged and frozenset({"research", "reseach"}) in merged
    assert not any("docker" in grp for grp in merged)                    # related is not the same


def test_merge_rewrites_files_updates_map_and_future_output(tagged):
    cfg, sd, files = tagged
    assert trun(ns(action="merge", tag="script-build", tags=[], into="build-script"), cfg) == 0
    assert tags_of(files["b.txt"]) == ["build-script"] and tags_of(files["a.txt"]) == ["build-script", "code"]
    assert cfgmod.user_value("tag_map") == {"script-build": "build-script"}
    tree = load_tree_for(cfg_now(cfg))
    assert tree.tag_map["script-build"] == "build-script" and "script-build" in tree.vocabulary    # still detected, emitted as the merged name
    done, skipped = __import__("sift.adapters.stores", fromlist=["undo"]).undo(sd / "undo.jsonl", [files["a.txt"].parent])
    assert tags_of(files["b.txt"]) == ["script-build"] and skipped == 0                                  # fully reversible


def test_merge_dry_run_changes_nothing(tagged):
    cfg, sd, files = tagged
    trun(ns(action="merge", tag="script-build", tags=[], into="build-script", dry_run=True), cfg)
    assert tags_of(files["b.txt"]) == ["script-build"] and cfgmod.user_value("tag_map") is None


def test_merge_into_new_tag_needs_description_or_a_vocabulary_source(tagged):
    cfg, sd, files = tagged
    assert trun(ns(action="merge", tag="data", tags=[], into="measurements"), cfg) == 2
    assert trun(ns(action="merge", tag="build-script", tags=["script-build"], into="building"), cfg) == 0
    assert "building" in cfgmod.user_value("vocabulary") and tags_of(files["a.txt"]) == ["building", "code"]


def test_rename_builtin_and_vocabulary_tags(tagged):
    cfg, sd, files = tagged
    assert trun(ns(action="rename", tag="data", into="measurements"), cfg) == 0
    assert tags_of(files["c.txt"]) == ["measurements"] and cfgmod.user_value("tag_map")["data"] == "measurements"
    assert trun(ns(action="rename", tag="build-script", into="build"), cfg) == 0
    assert tags_of(files["a.txt"]) == ["build", "code"] and "build" in cfgmod.user_value("vocabulary") and "build-script" not in cfgmod.user_value("vocabulary")


def test_delete_removes_from_files_and_stops_future_use(tagged):
    cfg, sd, files = tagged
    assert trun(ns(action="delete", tag="transcript"), cfg) == 0           # a packaged default tag
    assert "transcript" in cfgmod.user_value("disabled_tags")
    assert "transcript" not in load_tree_for(cfg_now(cfg)).vocabulary
    assert trun(ns(action="delete", tag="data"), cfg) == 0                 # a built-in label: dropped through the map
    assert tags_of(files["c.txt"]) == [] and cfgmod.user_value("tag_map")["data"] == ""


def test_merge_cycle_is_refused(tagged):
    cfg, sd, files = tagged
    trun(ns(action="merge", tag="script-build", tags=[], into="build-script"), cfg)
    assert trun(ns(action="merge", tag="build-script", tags=[], into="script-build"), cfg) == 1


def test_edit_and_add(tagged):
    cfg, sd, _ = tagged
    assert trun(ns(action="edit", tag="transcript", desc="spoken conversation", act=0.7), cfg) == 0
    v = cfgmod.user_value("vocabulary")["transcript"]
    assert v["description"] == "spoken conversation" and v["act"] == 0.7 and "require" in v     # the packaged gate is kept
    assert trun(ns(action="add", tag="Tax Return", desc="tax documents"), cfg) == 0
    assert "tax-return" in cfgmod.user_value("vocabulary")


# ---- review relevance and notifications ----------------------------------------------------------------------------------

def test_review_hides_generic_guesses_and_keeps_vocabulary_and_secrets(tmp_path, capsys):
    from sift import cli
    from sift.commands import review
    sd = state.state_dir()
    rows = [("a.md", ["transcript"], ""), ("b.txt", ["log"], ""), ("c.txt", ["code"], ""), ("d.pdf", [], "possible secret: not sent"), ("e.md", ["dutch"], "")]
    for i, (n, sugg, reason) in enumerate(rows):
        state.append(sd / "report.jsonl", {"path": str(tmp_path / n), "ref": [i, i, 10], "outcome": "review", "tags": [], "suggested": sugg,
                                          "reason": reason, "steps": [["root", "x", .5, "text"]], "v": 2})
    cfg = {"vocabulary": {"dutch": {"description": "the text is written in Dutch", "detector": "language:nl"}}}
    cli.cmd_review(Namespace(json=True, limit=50, all=False), cfg)
    names = [x["name"] for x in json.loads(capsys.readouterr().out)]
    assert sorted(names) == ["a.md", "d.pdf", "e.md"]
    cli.cmd_review(Namespace(json=True, limit=50, all=True), cfg)
    assert len(json.loads(capsys.readouterr().out)) == 5
    cli.cmd_status(Namespace(json=True), {**cfg, "endpoint": "http://127.0.0.1:1"})
    st = json.loads(capsys.readouterr().out)
    assert st["pending"] == 3 and st["hidden"] == 2


def test_notifications_are_throttled_and_opt_in(monkeypatch):
    from sift import notify
    sent = []
    monkeypatch.setattr(notify, "send", lambda t, b, u="normal": sent.append(t) or True)
    sd = state.state_dir()
    cfg = {}
    assert notify.after_index(cfg, sd, {}, aborted=True, first_pass_seen=False) == ["backend_down"]
    assert notify.after_index(cfg, sd, {}, aborted=True, first_pass_seen=False) == []                  # same outage: silent
    assert notify.after_index(cfg, sd, {"remaining": 5}, aborted=False, first_pass_seen=False) == []   # recovered quietly
    assert notify.after_index(cfg, sd, {}, aborted=True, first_pass_seen=False) == ["backend_down"]    # a new outage speaks again
    assert notify.after_index(cfg, sd, {"remaining": 0}, aborted=False, first_pass_seen=False) == ["first_pass"]
    assert notify.after_index(cfg, sd, {"remaining": 0}, aborted=False, first_pass_seen=False) == []
    assert notify.after_discover(cfg, sd, 2) == [] and notify.after_discover(cfg, sd, 5) == ["ideas"] and notify.after_discover(cfg, sd, 9) == []
    assert notify.after_index({"notifications": {"errors": False}}, sd, {}, True, False) == []
    assert len(sent) == 4
    assert notify.enabled({}, "review") is False                                                       # per-file style alerts are off by default


def test_store_key_writes_private_file_outside_config():
    path = setup.store_key("default", "  sk-123  ")
    assert Path(path).read_text() == "sk-123\n" and oct(Path(path).stat().st_mode & 0o777) == "0o600"
    assert oct(Path(path).parent.stat().st_mode & 0o777) == "0o700"
    b = setup.build_backend("typesafe", {"api_key_file": path, "consent": True})
    assert b["api_key_file"] == path and "sk-123" not in json.dumps(b)
    assert cfgmod.resolve_secret(b) == "sk-123"


def test_fresh_install_reports_unconfigured_not_an_error(capsys):
    from sift import cli
    from sift.commands import review
    cli.cmd_status(Namespace(json=True), cfgmod.load(Path("/nonexistent-root")))
    st = json.loads(capsys.readouterr().out)
    assert st["backend"]["state"] == "unconfigured" and st["pending"] == 0


# ---- tags that already exist on files, editing, and what merge/delete do -------------------------------------------------

@pytest.fixture
def foreign(tmp_path, xattr_required):
    """Files tagged by someone else (another tool, or by hand), unknown to Sift's report."""
    root = tmp_path / "mine"; root.mkdir()
    files = {}
    for name, tags in (("a.txt", ["holiday", "keep-me"]), ("b.txt", ["holiday"]), ("c.txt", ["Tax"])):
        p = root / name; p.write_text(name); os.setxattr(p, XATTR, ",".join(tags).encode()); files[name] = p
    return {"dirs": [str(root)]}, state.state_dir(), files


def test_tag_manager_reads_tags_that_already_exist_on_files(foreign):
    cfg, sd, files = foreign
    assert not any(r["tag"] == "holiday" for r in tagcmd.inventory(cfg_now(cfg), sd))        # before a scan: unknown
    assert trun(ns(action="scan"), cfg) == 0
    rows = {r["tag"]: r for r in tagcmd.inventory(cfg_now(cfg), sd)}
    assert rows["holiday"]["files"] == 2 and rows["holiday"]["kind"] == "yours" and rows["Tax"]["files"] == 1


def test_foreign_tags_can_be_renamed_merged_and_deleted_without_touching_other_tags(foreign):
    cfg, sd, files = foreign
    trun(ns(action="scan"), cfg)
    assert trun(ns(action="rename", tag="holiday", into="vacation"), cfg) == 0
    assert tags_of(files["a.txt"]) == ["keep-me", "vacation"] and tags_of(files["b.txt"]) == ["vacation"]
    trun(ns(action="scan"), cfg)
    assert trun(ns(action="merge", tag="vacation", tags=[], into="Tax"), cfg) == 2        # new name needs a description... Tax exists
    assert trun(ns(action="delete", tag="keep-me"), cfg) == 0
    assert tags_of(files["a.txt"]) == ["vacation"]


def test_files_edited_since_classification_are_still_handled(tagged):
    cfg, sd, files = tagged
    files["b.txt"].write_text("edited later with different size")                      # the report's file reference is now stale
    assert trun(ns(action="delete", tag="script-build"), cfg) == 0
    assert tags_of(files["b.txt"]) == []


def test_edit_description_and_rename_in_one_step(tagged):
    cfg, sd, files = tagged
    assert trun(ns(action="edit", tag="build-script", desc="  shell and make\nscripts that build things ", into="Build Tools"), cfg) == 0
    v = cfgmod.user_value("vocabulary")
    assert v["build-tools"]["description"] == "shell and make scripts that build things" and "build-script" not in v
    assert tags_of(files["a.txt"]) == ["build-tools", "code"]
    assert trun(ns(action="edit", tag="data", desc="measurements"), cfg) == 1              # built-in labels have no description
    assert trun(ns(action="edit", tag="build-tools", desc=""), cfg) == 1                    # empty description refused
    assert trun(ns(action="edit", tag="build-tools", require="(a+)+$"), cfg) == 1           # dangerous pattern refused
    assert trun(ns(action="edit", tag="build-tools", act=3.0), cfg) == 1


def test_rename_onto_an_existing_tag_is_refused(tagged):
    cfg, sd, _ = tagged
    assert trun(ns(action="rename", tag="build-script", into="script-build"), cfg) == 1


def test_merge_keeps_destination_description_and_source_definitions(tagged):
    cfg, sd, files = tagged
    trun(ns(action="merge", tag="script-build", tags=[], into="build-script"), cfg)
    tree = load_tree_for(cfg_now(cfg))
    assert tree.vocabulary["build-script"].description == "build scripts"                  # destination unchanged
    assert tree.vocabulary["script-build"].description == "scripts for builds"             # source still detects its own phrasing
    # a brand new destination borrows the first vocabulary source's description unless one is given
    trun(ns(action="merge", tag="build-script", tags=["script-build"], into="building", desc="anything about building"), cfg)
    assert cfgmod.user_value("vocabulary")["building"]["description"] == "anything about building"


def test_non_latin_tags_work_end_to_end(tagged):
    cfg, sd, files = tagged
    assert trun(ns(action="add", tag="税务", desc="税务文件、报税表和发票"), cfg) == 0
    assert cfgmod.user_value("vocabulary")["税务"]["description"] == "税务文件、报税表和发票"
    assert trun(ns(action="rename", tag="data", into="فاتورة"), cfg) == 0
    assert tags_of(files["c.txt"]) == ["فاتورة"]
    assert any(r["tag"] == "税务" for r in tagcmd.inventory(cfg_now(cfg), sd))


# ---- tag evaluation ----------------------------------------------------------------------------------------------------------

def test_tag_metrics_and_threshold_choice():
    from sift.commands import evaluate as ev
    scores = {f"p{i}": {"t": s} for i, s in enumerate([.95, .9, .85, .8, .75, .6, .55, .5, .45, .2])}
    truth = {f"p{i}": ({"t"} if i < 6 else set()) for i in range(10)}               # first six truly have the tag
    m = ev.tag_metrics(scores, truth, ["t"], (0.5, 0.7, 0.9))["t"]
    assert m["positives"] == 6
    assert m["by_threshold"][0.9]["precision"] == 1.0 and m["by_threshold"][0.9]["recall"] == pytest.approx(2 / 6)
    assert m["by_threshold"][0.5]["precision"] == pytest.approx(6 / 8) and m["by_threshold"][0.5]["recall"] == 1.0
    assert ev.best_threshold(m["by_threshold"], 0.95) == 0.7        # lowest threshold that reaches the precision target
    assert ev.best_threshold(m["by_threshold"], 1.01) is None


def test_template_and_truth_roundtrip(tmp_path, capsys):
    from sift.commands import evaluate as ev
    sd = state.state_dir()
    for i in range(6):
        p = tmp_path / f"f{i}.txt"; p.write_text(("invoice text for tax " * 20))
        st = os.stat(p)
        state.append(sd / "report.jsonl", {"path": str(p), "ref": [st.st_ino, st.st_mtime_ns, st.st_size], "outcome": "act", "v": 2,
                                          "tags": ["invoice"] if i < 3 else [], "suggested": ["contract"] if i == 3 else []})
    out = tmp_path / "labels.csv"
    assert ev.make_template({}, 10, out) >= 3
    text = out.read_text()
    assert "path,predicted,truth,note" in text and "?contract" in text and oct(out.stat().st_mode & 0o777) == "0o600"
    out.write_text("path,predicted,truth,note\nA,x,Invoice; 税务,\nB,x,,\n")
    assert ev.read_truth(out) == {"A": {"invoice", "税务"}, "B": set()}


# ---- preview and audit ---------------------------------------------------------------------------------------------------------

def test_wilson_lower_bound_is_honest_about_small_samples():
    from sift.commands.audit import wilson_lower
    assert wilson_lower(0, 0) == 0.0
    assert wilson_lower(10, 10) == pytest.approx(0.722, abs=0.01)         # 10 out of 10 only proves "at least ~72%"
    assert wilson_lower(90, 100) == pytest.approx(0.826, abs=0.01)
    assert wilson_lower(9, 10) < wilson_lower(90, 100)


def test_preview_only_opens_regular_files_in_scanned_folders(tmp_path, monkeypatch):
    from sift.commands import preview
    root = tmp_path / "scan"; root.mkdir(); inside = root / "a.pdf"; inside.write_text("x")
    outside = tmp_path / "secret.txt"; outside.write_text("x")
    link = root / "ln"; link.symlink_to(outside)
    opened = []
    monkeypatch.setattr(preview.subprocess, "Popen", lambda cmd, **kw: opened.append(cmd))
    monkeypatch.setattr(preview.shutil, "which", lambda n: None)                    # no hyprctl, no gtk-launch
    cfg = {"dirs": [str(root)]}
    assert preview.launch(cfg, str(inside)) == "opened a.pdf" and opened == [["xdg-open", str(inside)]]
    for bad in (outside, link, root, root / "missing"):
        with pytest.raises(Exception):
            preview.launch(cfg, str(bad))
    assert len(opened) == 1


def test_preview_app_overrides_per_type(tmp_path, monkeypatch):
    from sift.commands import preview
    p = tmp_path / "notes.md"
    monkeypatch.setattr(preview.shutil, "which", lambda n: "/usr/bin/" + n)
    assert preview.command_for({}, p) == ["xdg-open", str(p)]
    assert preview.command_for({"preview": {"apps": {"md": "code.desktop"}}}, p) == ["gtk-launch", "code", str(p)]
    assert preview.command_for({"preview": {"apps": {"md": ["glow", "-p", "{path}"]}}}, p) == ["glow", "-p", str(p)]
    assert preview.command_for({"preview": {"apps": {"*": "default"}}}, p) == ["xdg-open", str(p)]


def test_float_plan_targets_only_the_new_window_and_scales_to_the_monitor():
    from sift.commands import preview
    mon = [{"id": 0, "width": 3440, "height": 1440, "scale": 1.0}, {"id": 1, "width": 1920, "height": 1080, "scale": 1.0}]
    before = [{"address": "0xa", "monitor": 1, "floating": False}]
    after = before + [{"address": "0xb", "monitor": 1, "floating": False, "mapped": True}]
    steps = preview.plan_float(before, after, mon, [0.5, 0.5])
    assert steps[0]["lua"] == 'hl.dsp.window.float({ action = "enable", window = "address:0xb" })'
    assert steps[1]["lua"] == 'hl.dsp.window.resize({ x = 960, y = 540, window = "address:0xb" })'
    assert steps[1]["classic"] == ["resizewindowpixel", "exact 960 540,address:0xb"]
    assert "0xa" not in str(steps) and steps[-1]["lua"].startswith("hl.dsp.window.center")
    assert preview.plan_float(before, before, mon, [0.5, 0.5]) == []                   # a file opened in an existing window: nothing to float
    already = before + [{"address": "0xc", "monitor": 0, "floating": True}]
    assert not any("float" in s["lua"] for s in preview.plan_float(before, already, mon, [0.5, 0.5]))


def test_dispatch_falls_back_to_the_classic_syntax(monkeypatch):
    from sift.commands import preview
    calls = []

    class R:
        def __init__(self, out, rc=0): self.stdout, self.stderr, self.returncode = out, "", rc

    def fake_run(cmd, **kw):
        calls.append(cmd[2:])
        return R("error: expected a dispatcher") if cmd[2].startswith("hl.") else R("ok")
    monkeypatch.setattr(preview.subprocess, "run", fake_run)
    preview._dispatch({"lua": 'hl.dsp.window.float({})', "classic": ["setfloating", "address:0x1"]})
    assert calls == [['hl.dsp.window.float({})'], ["setfloating", "address:0x1"]]


def test_preview_settings_commands(tmp_path):
    from sift.commands import preview
    assert preview.run(ns(action="set", value="md", app="code"), {}) == 0
    assert cfgmod.user_value("preview")["apps"] == {"md": "code"}
    assert preview.run(ns(action="float", value="off"), {}) == 0 and cfgmod.user_value("preview")["float"] is False
    assert preview.run(ns(action="reset", value=".md"), preview_cfg()) == 0
    assert cfgmod.user_value("preview")["apps"] == {}


def preview_cfg():
    return cfgmod.load(Path("/nonexistent"))


def test_audit_picks_balanced_unjudged_files_and_marking_wrong_removes_the_tag(tagged):
    from sift.commands import audit
    cfg, sd, files = tagged
    items = audit.pick(cfg_now(cfg), sd, 10, seed=1)
    assert {i["name"] for i in items} == {"a.txt", "b.txt"} and all(i["tags"] for i in items)      # c.txt only has a built-in tag
    assert audit.mark(cfg, sd, str(files["a.txt"]), "build-script", "wrong") == "pruned"
    assert tags_of(files["a.txt"]) == ["code"]
    audit.mark(cfg, sd, str(files["b.txt"]), "script-build", "ok")
    assert audit.pick(cfg_now(cfg), sd, 10, seed=1) == []                                         # everything judged
    rows = {r["tag"]: r for r in audit.precision_table(audit.latest_verdicts(state.read(sd / "verdicts.jsonl")))}
    assert rows["build-script"]["precision"] == 0.0 and rows["script-build"]["precision"] == 1.0
    audit.mark(cfg, sd, str(files["c.txt"]), "build-script", "missed")                              # the tag should have been there
    assert "build-script" in tags_of(files["c.txt"])
    out = tmp_path_out = files["a.txt"].parent / "labels.csv"
    assert audit.export_labels(cfg_now(cfg), sd, out) == 3
    rows = {Path(l.split(",")[0]).name: l for l in out.read_text().splitlines()[1:]}
    assert ",build-script," in rows["c.txt"] and ",script-build," in rows["b.txt"] and rows["a.txt"].split(",")[2] == ""
    assert __import__("sift.adapters.stores", fromlist=["undo"]).undo(sd / "undo.jsonl", [files["a.txt"].parent])[0] >= 1


def test_audit_balances_across_tags(tmp_path):
    from sift.commands import audit
    root = tmp_path / "f"; root.mkdir(); sd = state.state_dir()
    cfgmod.save_user({"vocabulary": {"aaa": {"description": "a"}, "bbb": {"description": "b"}}})
    for i in range(20):
        p = root / f"{i}.txt"; p.write_text("x"); st = os.stat(p)
        state.append(sd / "report.jsonl", {"path": str(p), "ref": [st.st_ino, st.st_mtime_ns, st.st_size], "outcome": "act", "v": 2,
                                          "tags": ["aaa"] if i < 18 else ["bbb"], "suggested": []})
    got = audit.pick({"dirs": [str(root)], **cfgmod.load(Path("/nonexistent"))}, sd, 6, seed=3)
    tags = [t for i in got for t in i["tags"]]
    assert tags.count("bbb") >= 2                                                                   # the rare tag is not drowned out


# ---- confirmations --------------------------------------------------------------------------------------------------------------

def test_destructive_tag_changes_need_confirmation_when_not_interactive(tagged, capsys):
    cfg, sd, files = tagged
    no = lambda **kw: ns(yes=False, **kw)
    assert trun(no(action="delete", tag="script-build"), cfg) == 3                                   # asked, nobody to answer: nothing changes
    err = capsys.readouterr().err
    assert "removes it from 1 files" in err and "no file is deleted" in err and "sift untag" in err and "--yes" in err
    assert tags_of(files["b.txt"]) == ["script-build"] and cfgmod.user_value("tag_map") is None
    assert trun(no(action="merge", tag="script-build", tags=[], into="build-script"), cfg) == 3
    assert "1 files carrying it will get 'build-script'" in capsys.readouterr().err
    assert trun(no(action="rename", tag="build-script", into="builder"), cfg) == 3
    assert "rewrites the tags of 1 files" in capsys.readouterr().err and tags_of(files["a.txt"]) == ["build-script", "code"]
    assert trun(no(action="delete", tag="script-build", dry_run=True), cfg) == 0                     # a preview never asks
    assert trun(ns(yes=True, action="delete", tag="script-build"), cfg) == 0 and tags_of(files["b.txt"]) == []


def test_interactive_answer_decides(tagged, monkeypatch):
    cfg, sd, files = tagged
    monkeypatch.setattr("sys.stdin", type("T", (), {"isatty": lambda self: True, "readline": lambda self: "n\n"})())
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")
    assert trun(ns(yes=False, action="delete", tag="script-build"), cfg) == 3 and tags_of(files["b.txt"]) == ["script-build"]
    monkeypatch.setattr("builtins.input", lambda prompt="": "y")
    assert trun(ns(yes=False, action="delete", tag="script-build"), cfg) == 0 and tags_of(files["b.txt"]) == []


def test_nothing_to_change_does_not_ask_for_rename_or_merge(tagged):
    cfg, sd, _ = tagged
    assert trun(ns(yes=False, action="rename", tag="transcript", into="speech"), cfg) == 0             # no file carries it


def test_secret_store_sends_the_key_on_stdin_never_in_arguments(monkeypatch):
    from sift.commands import secret
    seen = {}
    monkeypatch.setattr(secret, "available", lambda: True)
    monkeypatch.setattr(secret.subprocess, "run", lambda cmd, **kw: seen.update(cmd=cmd, **kw))
    secret.store("default", "  sk-super-secret \n")
    assert seen["input"] == "sk-super-secret" and "sk-super-secret" not in " ".join(seen["cmd"])
    assert seen["cmd"][:3] == ["secret-tool", "store", "--label"] and seen["cmd"][-4:] == ["service", "sift", "backend", "default"]
    secret.attach("default")
    assert cfgmod.user_value("backends")["default"] == {"api_key_keyring": "default"}
    with pytest.raises(cfgmod.ConfigError):
        secret.store("default", "   ")


def test_a_splash_screen_does_not_hide_the_real_window(monkeypatch):
    """LibreOffice shows a splash window first and the document window ~1.5 s later: both must end up floating."""
    from sift.commands import preview
    mon = [{"id": 0, "width": 2000, "height": 1000, "scale": 1.0}]
    timeline = [[], [{"address": "0xsplash", "monitor": 0, "floating": False, "mapped": True}],
                [{"address": "0xsplash", "monitor": 0, "floating": True, "mapped": True}],
                [{"address": "0xsplash", "monitor": 0, "floating": True, "mapped": True},
                 {"address": "0xdoc", "monitor": 0, "floating": False, "mapped": True}]]
    state_ = {"i": -1}
    dispatched = []

    def fake_hypr(what):
        if what == "monitors":
            return mon
        state_["i"] = min(state_["i"] + 1, len(timeline) - 1)
        return timeline[state_["i"]]
    t = {"now": 0.0}
    monkeypatch.setattr(preview, "_hypr", fake_hypr)
    monkeypatch.setattr(preview, "_dispatch", lambda step: dispatched.append(step["lua"]))
    known = preview.float_new({"0xold"}, {"size": [0.5, 0.5]}, 5, sleep=lambda s: t.__setitem__("now", t["now"] + s), clock=lambda: t["now"])
    assert known == {"0xold", "0xsplash", "0xdoc"}
    assert any('address:0xsplash' in d and "float" in d for d in dispatched) and any('address:0xdoc' in d and "float" in d for d in dispatched)
    assert sum('address:0xsplash' in d and "float(" in d for d in dispatched) == 1                  # an address is handled once


def test_launch_returns_after_the_first_window_and_hands_over_to_a_follower(tmp_path, monkeypatch):
    from sift.commands import preview
    root = tmp_path / "s"; root.mkdir(); f = root / "a.docx"; f.write_text("x")
    spawned = []
    monkeypatch.setattr(preview.subprocess, "Popen", lambda cmd, **kw: spawned.append(cmd))
    monkeypatch.setattr(preview.shutil, "which", lambda n: "/usr/bin/" + n)
    monkeypatch.setattr(preview, "_hypr", lambda what: [{"address": "0xold"}] if what == "clients" else [])
    monkeypatch.setattr(preview, "float_new", lambda known, st, seconds, stop_after_first=False: known | {"0xnew"})
    preview.launch({"dirs": [str(root)]}, str(f))
    assert spawned[0][0] in ("xdg-open", "gtk-launch")
    assert spawned[1][1:4] == ["preview", "follow", "0xnew,0xold"] and spawned[1][4] == "25.0"


# ---- demo mode -------------------------------------------------------------------------------------------------------------------

def test_demo_runs_the_real_program_on_invented_data_without_touching_real_state(tmp_path, monkeypatch, capsys):
    from sift import cli, demo
    real_cfg = cfgmod.user_config_path()                                         # captured before demo mode redirects the paths
    monkeypatch.setenv("SIFT_DEMO", "1")
    monkeypatch.setenv("SIFT_DEMO_HOME", str(tmp_path / "demo"))
    demo.build()
    assert cfgmod.config_dir() == tmp_path / "demo" / "config" and state.state_dir() == tmp_path / "demo" / "state"
    cfg = cfgmod.load()
    assert [str(d) for d in cfgmod.dirs(cfg)] == [str(tmp_path / "demo" / "files")] and cfg["backends"]["default"]["type"] == "demo"
    assert cli.main(["status", "--json"]) == 0
    st = json.loads(capsys.readouterr().out)
    assert st["demo"] is True and st["backend"]["state"] == "ok" and st["pending"] >= 4 and st["hidden"] >= 5 and st["ideas"] == 4
    assert any(b["name"] == "hosted" and not b["local"] and b["key"] == "keyring" for b in st["backends"]) and st["usage"]["week"] > 0
    assert cli.main(["tags", "list", "--json"]) == 0
    tags = {r["tag"]: r for r in json.loads(capsys.readouterr().out)}
    assert tags["税务"]["files"] >= 1 and tags["فاتورة"]["files"] >= 1 and tags["invoice"]["files"] >= 2
    assert not real_cfg.exists()                                                  # the real config was never created


def test_demo_actions_work_on_the_sandbox_and_system_changes_are_refused(tmp_path, monkeypatch, capsys):
    from sift import cli, demo
    monkeypatch.setenv("SIFT_DEMO", "1"); monkeypatch.setenv("SIFT_DEMO_HOME", str(tmp_path / "demo"))
    demo.build()
    f = next((tmp_path / "demo" / "files" / "Taxes").glob("receipt-printing*"))
    assert cli.main(["tags", "delete", "invoice", "--yes"]) == 0 and "invoice" not in (os.getxattr(f, XATTR).decode() if XATTR in os.listxattr(f) else "")
    for argv in (["setup", "--preset", "open-jev", "--yes"], ["schedule", "set", "1h"], ["secret", "set", "x"]):
        assert cli.main(argv) == 2 and "demo mode" in capsys.readouterr().err
    assert cli.main(["review", "--json"]) == 0 and json.loads(capsys.readouterr().out)
    assert cli.main(["index", "--budget", "5"]) == 0                                # the offline classifier lets a scan run


def test_demo_on_off_marker(tmp_path, monkeypatch):
    from sift import demo
    monkeypatch.setenv("SIFT_DEMO_HOME", str(tmp_path / "demo"))
    monkeypatch.delenv("SIFT_DEMO")                       # the suite forces it off; this test is about the marker
    assert not demo.active()
    from sift.commands import demo_cmd
    demo_cmd.run(Namespace(action="on", json=False), {})
    assert demo.active()
    demo_cmd.run(Namespace(action="off", json=False), {})
    assert not demo.active()


# ---- review: your own tags and learning from review decisions ---------------------------------------------------------

def _review_setup(tmp_path):
    d = tmp_path / "docs"; d.mkdir()
    f = d / "scan.txt"; f.write_text("Hello there, a note about my trip to Lisbon. " * 20)
    cfgmod.save_user({"dirs": [str(d)], "write_xattrs": True})
    return f


@pytest.mark.xattr
def test_add_tag_new_with_description_joins_vocabulary_and_records_verdict(tmp_path):
    from sift.commands import addtag
    f = _review_setup(tmp_path)
    cfg = cfgmod.load(tmp_path / "no-defaults")
    r = addtag.add_tag(cfg, state.state_dir(), str(f), "Travel", "notes about trips and holidays")
    assert r == {"tag": "travel", "new": True, "learned": True, "suggested": False}
    assert os.getxattr(f, XATTR) == b"travel"
    assert load_tree_for(cfgmod.load(tmp_path / "no-defaults")).vocabulary["travel"].description.startswith("notes about")
    v = state.read(state.state_dir() / "verdicts.jsonl")[-1]
    assert (v["tag"], v["verdict"], v["source"]) == ("travel", "missed", "review")
    assert state.read(state.state_dir() / "reviews.jsonl")[-1]["manual"] is True


@pytest.mark.xattr
def test_add_tag_existing_without_description_and_bad_input(tmp_path):
    from sift.commands import addtag
    f = _review_setup(tmp_path)
    cfg = cfgmod.load(tmp_path / "no-defaults")
    r = addtag.add_tag(cfg, state.state_dir(), str(f), "invoice", None)
    assert r["new"] is False and r["learned"] is True
    r = addtag.add_tag(cfg, state.state_dir(), str(f), "my-own", None)
    assert r["new"] is True and r["learned"] is False and "my-own" not in load_tree_for(cfgmod.load(tmp_path / "no-defaults")).vocabulary
    from sift.validate import ValidationError
    with pytest.raises(ValidationError):
        addtag.add_tag(cfg, state.state_dir(), str(f), "", None)
    t = addtag.add_tag(cfg, state.state_dir(), str(f), "../../x", None)["tag"]
    assert "/" not in t and ".." not in t                  # normalised, never a path


def test_add_tag_refuses_secrets(tmp_path):
    from sift.adapters.stores import StoreError
    from sift.commands import addtag
    f = _review_setup(tmp_path)
    f.write_text("-----BEGIN RSA PRIVATE KEY-----\nabc\n-----END RSA PRIVATE KEY-----\n")
    with pytest.raises(StoreError):
        addtag.add_tag(cfgmod.load(tmp_path / "no-defaults"), state.state_dir(), str(f), "keys", None)


# ---- merge / unmerge and merged suggestions ----------------------------------------------------------------------------

def _tagged(tmp_path, names_tags):
    d = tmp_path / "m"; d.mkdir()
    cfgmod.save_user({"dirs": [str(d)], "write_xattrs": True})
    out = {}
    for n, tags in names_tags.items():
        f = d / n; f.write_text("x"); os.setxattr(f, XATTR, ",".join(tags).encode()); out[n] = f
    return out


def _report(files):
    from sift.adapters.extract import stat_ref
    for f in files:
        r = stat_ref(f)
        state.append(state.state_dir() / "report.jsonl", {"path": str(f), "ref": [r.inode, r.mtime_ns, r.size], "outcome": "act", "tags": [], "suggested": [], "v": 2, "steps": []})


def _tags_cmd(action, tag=None, **kw):
    return tagcmd.run(ns(action=action, tag=tag, into=kw.pop("into", None), tags=kw.pop("tags", []), **kw), cfgmod.load(Path("/nonexistent")))


@pytest.mark.xattr
def test_unmerge_restores_the_source_tag_and_drops_a_destination_that_came_only_from_the_merge(tmp_path):
    fs = _tagged(tmp_path, {"a.txt": ["colour"], "b.txt": ["color", "colour"]})
    _report(fs.values())
    assert _tags_cmd("merge", "colour", into="color", desc="a colour", yes=True) == 0
    assert os.getxattr(fs["a.txt"], XATTR) == b"color" and os.getxattr(fs["b.txt"], XATTR) == b"color"
    assert _tags_cmd("unmerge", "colour", yes=True) == 0
    assert os.getxattr(fs["a.txt"], XATTR) == b"colour"                       # destination came only from the merge
    assert sorted(os.getxattr(fs["b.txt"], XATTR).decode().split(",")) == ["color", "colour"]   # it had both before
    assert "colour" not in (cfgmod.load(Path("/nonexistent")).get("tag_map") or {})
    assert _tags_cmd("unmerge", "colour", yes=True) == 1                      # no longer merged


@pytest.mark.xattr
def test_unmerge_best_effort_for_merges_made_before_the_journal(tmp_path):
    fs = _tagged(tmp_path, {"a.txt": ["travel"]})
    from sift.adapters.extract import stat_ref
    r = stat_ref(fs["a.txt"])
    state.append(state.state_dir() / "report.jsonl", {"path": str(fs["a.txt"]), "ref": [r.inode, r.mtime_ns, r.size], "outcome": "act", "tags": ["trip"], "suggested": [], "v": 2, "steps": []})
    cfgmod.save_user({"tag_map": {"trip": "travel"}})
    assert _tags_cmd("unmerge", "trip", yes=True) == 0
    assert sorted(os.getxattr(fs["a.txt"], XATTR).decode().split(",")) == ["travel", "trip"]


@pytest.mark.xattr
def test_merged_suggestions_are_not_shown_or_written(tmp_path):
    from sift import cli
    from sift.commands import review
    fs = _tagged(tmp_path, {"a.txt": [], "b.txt": ["travel"]})
    from sift.adapters.extract import stat_ref
    for n, f in fs.items():
        r = stat_ref(f)
        state.append(state.state_dir() / "report.jsonl", {"path": str(f), "ref": [r.inode, r.mtime_ns, r.size], "outcome": "review", "tags": [], "suggested": ["trip"], "v": 2, "steps": [["root", "document", .9, "text"]], "scores": {"trip": .6}})
    cfgmod.save_user({"tag_map": {"trip": "travel"}})
    cfg = cfgmod.load(Path("/nonexistent"))
    assert review._effective(state.latest_by_path(state.read(state.state_dir() / "report.jsonl"))[str(fs["a.txt"])], cfg) == ["travel"]     # shows the destination
    assert review._effective(state.latest_by_path(state.read(state.state_dir() / "report.jsonl"))[str(fs["b.txt"])], cfg) == []             # already has it
    groups, _ = review._review_groups(cfg, True)
    assert [Path(g[0]["path"]).name for g in groups] == ["a.txt"]


def test_merged_source_disappears_from_the_tag_list():
    cfgmod.save_user({"vocabulary": {"colour": {"description": "x"}}, "tag_map": {"colour": "color"}})
    rows = tagcmd.inventory(cfgmod.load(Path("/nonexistent")), state.state_dir())
    assert "colour" not in {r["tag"] for r in rows}


def test_dismissed_merge_proposals_stay_dismissed_even_when_the_group_grows():
    names = {"colour": 3, "color": 5, "colours": 1}
    assert len(tagcmd.similar_groups(names)) == 1
    g = tagcmd.similar_groups(names)[0]
    assert tagcmd.similar_groups(names, ignored=[["color", "colour", "colours"]]) == []
    # one pair dismissed: the others can still be proposed together
    part = tagcmd.similar_groups(names, ignored=[["color", "colour"]])
    assert all(not ({"color", "colour"} <= set(x["merge"] + [x["keep"]])) for x in part)


def test_ignore_merge_command_persists_and_resets(capsys):
    assert _tags_cmd("ignore-merge", "colour", tags=["color"]) == 0
    assert cfgmod.load(Path("/nonexistent"))["ignored_merges"] == [["colour", "color"] and sorted(["colour", "color"])]
    assert _tags_cmd("ignore-merge", "colour", tags=["color"]) == 0                     # idempotent
    assert len(cfgmod.load(Path("/nonexistent"))["ignored_merges"]) == 1
    assert _tags_cmd("ignore-merge", "onlyone") == 2
    assert _tags_cmd("ignore-merge", reset=True) == 0 and cfgmod.load(Path("/nonexistent"))["ignored_merges"] == []
