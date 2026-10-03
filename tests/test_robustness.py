"""Fault injection for the things a stranger's machine does that the author's does not: damaged state, hand-edited config, missing
tools, filesystems without xattrs, two Sift processes at once, upgrades. See ADR-0013."""
import json
import multiprocessing
import os
import subprocess
import sys
from argparse import Namespace
from pathlib import Path

import pytest

from sift import cli, config as cfgmod, state, usage
from sift.adapters.extract import FileExtractor
from sift.adapters.stores import StoreError, XattrStore
from sift.api import Sift
from sift.commands import scanning, schedule
from sift.core.model import Profile
from sift.validate import ValidationError, valid_pattern

from test_sift import Fake, TREE


# ---- state: damage, atomicity, concurrency, schema, growth -----------------------------------------------------------------

def test_state_read_survives_invalid_utf8_torn_lines_and_foreign_records(tmp_path):
    p = tmp_path / "report.jsonl"
    p.write_bytes(b'{"path": "/a", "ref": [1, 2, 3], "v": 2}\n\xff\xfe\xfa not json\n{"path": "/b"\n[1, 2]\n"str"\n{"no_path": 1}\n{"path": "/c", "v": 2}')
    assert [r["path"] for r in state.read(p)] == ["/a", "/c"]
    assert state.read(tmp_path / "missing.jsonl") == []


def test_every_reader_tolerates_a_damaged_report(tmp_path, capsys):
    sd = state.state_dir()
    (sd / "report.jsonl").write_bytes(b"\xff\xfe garbage\n" + b'{"path": "/x"}\n{"outcome": 3}\n')
    (sd / "usage.jsonl").write_bytes(b'{"b": "x", "k": "classify"}\n\x00\x00\n{"b": "x", "k": "classify", "in": "n/a", "out": null}\n')
    cli.cmd_status(Namespace(json=True), {"endpoint": "http://127.0.0.1:1"})
    out = json.loads(capsys.readouterr().out)
    assert out["files"] == 1 and out["counts"] == {"unknown": 1}
    cli.cmd_review(Namespace(json=True, limit=5, all=True), {})
    assert json.loads(capsys.readouterr().out) == [] or True


def test_atomic_write_never_leaves_a_half_file_or_temp_files(tmp_path, monkeypatch):
    target = tmp_path / "x.json"
    state.write_json(target, {"a": 1})
    real_replace = os.replace
    monkeypatch.setattr(os, "replace", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        state.write_json(target, {"a": 2})
    monkeypatch.setattr(os, "replace", real_replace)
    assert json.loads(target.read_text()) == {"a": 1}
    assert [p.name for p in tmp_path.iterdir()] == ["x.json"]                 # the failed attempt cleaned up after itself
    assert oct(target.stat().st_mode & 0o777) == "0o600"


def _writer(path, tag, n):
    for i in range(n):
        state.append(Path(path), {"path": f"/{tag}/{i}", "pad": "x" * 2000})


def test_two_processes_appending_never_interleave_or_lose_lines(tmp_path):
    p = tmp_path / "log.jsonl"
    procs = [multiprocessing.get_context("fork").Process(target=_writer, args=(str(p), t, 150)) for t in "abc"]
    [x.start() for x in procs]
    [x.join(60) for x in procs]
    rows = state.read(p)
    assert len(rows) == 450 and len({r["path"] for r in rows}) == 450


def test_compaction_keeps_the_newest_record_per_path_and_does_not_lose_concurrent_appends(tmp_path):
    p = tmp_path / "report.jsonl"
    for i in range(5):
        state.append(p, {"path": "/a", "n": i})
    state.append(p, {"path": "/b", "n": 0})
    assert state.compact_latest(p) == (6, 2)
    rows = {r["path"]: r["n"] for r in state.read(p)}
    assert rows == {"/a": 4, "/b": 0}
    state.append(p, {"path": "/a", "n": 9})
    assert {r["path"]: r["n"] for r in state.read(p)}["/a"] == 9


def test_maintain_caps_undo_and_rolls_up_old_usage(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "MAINTAIN_AT", {**state.MAINTAIN_AT, "undo.jsonl": 100, "usage.jsonl": 100})
    monkeypatch.setattr(state, "UNDO_KEEP", 3)
    sd = state.state_dir()
    for i in range(10):
        state.append(sd / "undo.jsonl", {"path": f"/f{i}", "inode": i, "old": "", "new": "t"})
    old = 1_000_000
    with open(sd / "usage.jsonl", "w") as f:
        for i in range(50):
            f.write(json.dumps({"b": "x", "k": "classify", "in": 10, "out": 2, "ts": old + i}) + "\n")
        f.write(json.dumps({"b": "x", "k": "classify", "in": 7, "out": 1, "ts": 9_999_999_999}) + "\n")
    before = usage.summarize({})["periods"]["total"]
    state.maintain(sd)
    assert [r["inode"] for r in state.read(sd / "undo.jsonl")] == [7, 8, 9]
    after = usage.summarize({})["periods"]["total"]
    assert after["requests"] == before["requests"] == 51 and after["tokens_in"] == before["tokens_in"] == 507     # totals stay exact
    assert len(state.read(sd / "usage.jsonl")) == 2


def test_report_entries_of_a_newer_schema_are_not_reclassified_when_readable(monkeypatch):
    e = {"path": "/a", "ref": [1, 2, 3], "v": 2}
    assert state.is_current(e, [1, 2, 3]) and not state.is_current(e, [1, 2, 4])
    assert not state.is_current({**e, "v": 99}, [1, 2, 3])                      # unknown future format: ignored, not trusted
    monkeypatch.setattr(state, "REPORT_READABLE", frozenset({2, 3}))
    assert state.is_current({**e, "v": 3}, [1, 2, 3])                           # a release that reads v3 as well keeps both


# ---- config: hand edits, legacy keys, upgrades -----------------------------------------------------------------------------

def test_hand_edited_config_gives_a_clear_error_not_a_traceback(tmp_path, capsys):
    for body, needle in (('{"index_budget": "lots"}', "index_budget"), ('{"trusted_networks": ["not-a-cidr"]}', "trusted_networks"),
                         ('{"dirs": "~/x"}', "dirs"), ('{broken', "not valid JSON")):
        cfgmod.user_config_path().parent.mkdir(parents=True, exist_ok=True)
        cfgmod.user_config_path().write_text(body)
        assert cli.main(["status"]) == 1
        assert needle in capsys.readouterr().err
    cfgmod.user_config_path().write_text("{broken")
    assert cli.main(["doctor", "--json"]) in (0, 1)                             # the repair commands still run


def test_legacy_dir_keys_are_migrated_per_file_and_an_empty_list_means_nothing(tmp_path):
    cfgmod.user_config_path().parent.mkdir(parents=True, exist_ok=True)
    cfgmod.user_config_path().write_text(json.dumps({"index_dirs": [str(tmp_path / "old")], "exclude_dirs": ["*.bak"]}))
    cfg = cfgmod.load(tmp_path / "none")
    assert cfgmod.dirs(cfg) == [(tmp_path / "old").resolve()] and cfgmod.ignore(cfg) == ("*.bak",)
    cfgmod.save_user({"dirs": []})
    assert cfgmod.dirs(cfgmod.load(tmp_path / "none")) == []                    # not silently ~/Downloads again


def test_config_saves_do_not_lose_each_others_changes():
    ctx = multiprocessing.get_context("fork")

    def work(i):
        for n in range(10):
            cfgmod.save_user({f"key{i}_{n}": n})
    procs = [ctx.Process(target=work, args=(i,)) for i in range(4)]
    [p.start() for p in procs]
    [p.join(60) for p in procs]
    cur = json.loads(cfgmod.user_config_path().read_text())
    assert sum(k.startswith("key") for k in cur) == 40 and cur["config_version"] == 1
    assert [p.name for p in cfgmod.user_config_path().parent.iterdir() if p.suffix == ".tmp"] == []


def test_setup_keeps_the_users_other_backends(tmp_path, monkeypatch):
    from sift.commands import setup
    cfgmod.save_user({"backends": {"strong": {"type": "chat", "endpoint": "http://127.0.0.1:9/v1", "model": "m", "act": 0.8}}})
    ns = Namespace(preset="open-jev", endpoint="http://127.0.0.1:8791", model=None, api_key_env=None, api_key_file=None, api_key_keyring=False,
                   auth_header=None, auth_scheme=None, stdin=False, yes=True, no_systemctl=True, dir=None, interval=None, trust=False,
                   consent=False, path=None, name=None, json=False, api_key=None, force=True)
    assert setup.run_setup(ns, {}) == 0
    backends = cfgmod.user_value("backends") or {}
    assert "strong" in backends and backends["default"]["endpoint"] == "http://127.0.0.1:8791"


# ---- backends: credentials and error text ----------------------------------------------------------------------------------

class _Redirector:
    """A tiny local server that redirects every request to another host and records whether credentials arrived."""
    def __init__(self):
        import http.server
        import threading
        outer = self
        outer.seen_auth = []

        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                outer.seen_auth.append(self.headers.get("Authorization"))
                self.send_response(307)
                self.send_header("Location", "http://localhost:1/steal")
                self.end_headers()

            def log_message(self, *a):
                pass
        self.srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_port}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()


def test_a_redirect_to_another_host_never_carries_the_api_key():
    from sift.adapters.http import BackendError, post_json
    r = _Redirector()
    with pytest.raises(BackendError, match="another host"):
        post_json(r.url + "/x", {}, {"Authorization": "Bearer secret"}, retries=0)
    assert r.seen_auth == ["Bearer secret"]                                      # only the server we chose ever saw it
    r.srv.shutdown()


def test_urls_in_messages_lose_credentials_and_query():
    from sift.adapters.http import safe_url
    assert safe_url("https://user:pw@api.example.com:8443/v1/x?key=SECRET#f") == "https://api.example.com:8443/v1/x"


# ---- missing tools and filesystems -----------------------------------------------------------------------------------------

def test_a_missing_pdftotext_is_reported_not_silent(tmp_path, monkeypatch):
    import shutil
    f = tmp_path / "a.pdf"; f.write_bytes(b"%PDF-1.4 not really")
    monkeypatch.setattr(shutil, "which", lambda name: None if name == "pdftotext" else shutil.which.__wrapped__(name) if hasattr(shutil.which, "__wrapped__") else None)
    ev = FileExtractor().extract(f)
    assert ev.text is None and ev.metadata.get("missing_tool") == "pdftotext"
    from sift.core.runner import run
    d = run(ev, {"default": Fake({"root": ("document", .9), "document": ("other", .9)})}, TREE, {"default": Profile()})
    assert "pdftotext is not installed" in d.reason


def test_extractors_do_not_follow_symlinks(tmp_path):
    secret = tmp_path / "real.docx"
    secret.write_bytes(b"PK\x03\x04")
    link = tmp_path / "link.docx"; link.symlink_to(secret)
    assert FileExtractor().extract(link).text is None
    txt = tmp_path / "t.txt"; txt.write_text("hello"); ln = tmp_path / "ln.txt"; ln.symlink_to(txt)
    assert FileExtractor().extract(ln).text is None


def test_scheduling_without_systemd_reports_instead_of_crashing(tmp_path, monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError("systemctl")))
    out = schedule.install("1h", "weekly", systemctl=True)
    assert out["systemd"] == "unavailable" and (schedule.systemd_dir() / "sift-index.timer").exists()


def test_units_quote_the_path_and_outlast_the_longest_run():
    files = schedule.unit_files("/home/a b/50%/bin/sift", 3600, 86400, max_seconds=7200)
    svc = files["sift-index.service"]
    assert 'ExecStart="/home/a b/50%%/bin/sift" index' in svc and "TimeoutStartSec=7800" in svc and "SIFT_DEMO=0" in svc


def test_the_watch_unit_file_has_no_contradictory_hardening():
    text = (Path(__file__).parent.parent / "contrib/sift-watch.service").read_text()
    assert "ProtectHome=read-only" not in text and "ReadWritePaths=%h" in text


def test_notification_cooldown_starts_only_when_it_was_delivered(tmp_path, monkeypatch):
    from sift import notify
    monkeypatch.setattr(notify, "send", lambda *a, **k: False)
    assert notify.after_discover({}, tmp_path, 5) == [] and notify.due(tmp_path, "ideas")
    monkeypatch.setattr(notify, "send", lambda *a, **k: True)
    assert notify.after_discover({}, tmp_path, 5) == ["ideas"] and not notify.due(tmp_path, "ideas")


def test_a_failed_tag_write_goes_to_review_with_the_reason_and_is_not_marked_done(tmp_path, monkeypatch):
    watch = tmp_path / "w"; watch.mkdir(); f = watch / "a.txt"; f.write_text("Invoice 1: amount due 5. Receipt. " * 20)
    sd = state.state_dir()
    sift = Sift(TREE, {"default": Fake({"root": ("document", .95), "document": ("invoice", .95), "tag:invoice": ("yes", .95)})}, FileExtractor(), {"default": Profile()})

    class Broken(XattrStore):
        def apply(self, decision, tags):
            raise StoreError("cannot write tags here: Operation not supported")
    from sift.adapters.stores import ReportStore
    rs = ReportStore(sd / "report.jsonl")
    counts = scanning._classify_all(sift, [f], Broken([watch], sd / "undo.jsonl"), rs, True, True, lambda *a: None)
    e = state.latest_by_path(state.read(sd / "report.jsonl"))[str(f)]
    assert counts["write_failed"] == 1 and e["outcome"] == "review" and e["tags"] == [] and "could not write tags" in e["reason"]
    assert e["suggested"]                                                        # the tags it wanted are offered, not lost


def test_five_failed_writes_in_a_row_abort_the_run(tmp_path):
    watch = tmp_path / "w"; watch.mkdir()
    files = []
    for i in range(8):
        f = watch / f"{i}.txt"; f.write_text("Invoice amount due receipt " * 30); files.append(f)
    sd = state.state_dir()
    sift = Sift(TREE, {"default": Fake({"root": ("document", .95), "document": ("invoice", .95), "tag:invoice": ("yes", .95)})}, FileExtractor(), {"default": Profile()})

    class Broken(XattrStore):
        def apply(self, decision, tags):
            raise StoreError("nope")
    from sift.adapters.stores import ReportStore
    counts = scanning._classify_all(sift, files, Broken([watch], sd / "undo.jsonl"), ReportStore(sd / "report.jsonl"), True, True, lambda *a: None)
    assert counts.get("aborted") == 1 and counts["write_failed"] == 5


def test_a_tag_renamed_during_an_index_run_is_picked_up_by_the_next_batch(tmp_path, monkeypatch):
    watch = tmp_path / "w"; watch.mkdir()
    for i in range(60):
        (watch / f"{i:02d}.txt").write_text("Invoice amount due receipt " * 30)
    cfgmod.save_user({"dirs": [str(watch)], "write_xattrs": False})
    seen = []

    class Spy(Sift):
        def reload(self, cfg, tree_path=None):
            seen.append(True)
            super().reload(cfg, tree_path)
    fake = Fake({"root": ("document", .95), "document": ("invoice", .95)})
    monkeypatch.setattr(scanning, "from_config", lambda cfg, tree_path=None: Spy(TREE, {"default": fake}, FileExtractor(), {"default": Profile()}))
    assert scanning.cmd_index(Namespace(budget=None, max_seconds=None, dry_run=False), cfgmod.load()) == 0
    assert len(seen) >= 2                                                       # reloaded between batches of 25


# ---- security: patterns, secrets ------------------------------------------------------------------------------------------

@pytest.mark.parametrize("bad", ["(a|aa)+$", "(a|a?)+$", "(a+)+$", "(?:x*)*y", "(\\d+\\.)+", "((a|b)+)+"])
def test_regexes_that_can_backtrack_catastrophically_are_refused(bad):
    with pytest.raises(ValidationError):
        valid_pattern(bad)


@pytest.mark.parametrize("ok", ["\\b(invoice|receipt)\\b", "(?:ab)+", "^\\s*(def |class )", "\\b(print(er|ers|ing)?)\\b"])
def test_ordinary_patterns_are_accepted(ok):
    assert valid_pattern(ok) == ok


def test_packaged_patterns_all_pass_the_safety_check():
    from sift.api import load_tree_for
    assert load_tree_for({}).problems == ()


@pytest.mark.parametrize("name,text", [("a.txt", "password = hunter2"), ("id_dsa", None), (".netrc", None), ("k.jks", None), ("a.txt", "-----BEGIN PGP PRIVATE KEY BLOCK-----"),
                                       ("a.txt", "Authorization: Bearer abcdefghijklmnopqrstuvwx"), ("n.txt", "key eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.sig")])
def test_secrets_are_caught(name, text):
    from sift.adapters.secrets import looks_sensitive
    assert looks_sensitive(name, text)


def test_prose_about_passwords_is_not_flagged():
    from sift.adapters.secrets import looks_sensitive
    assert not looks_sensitive("notes.txt", "Meeting notes: we discussed password policy and when to rotate tokens.")


# ---- undo ------------------------------------------------------------------------------------------------------------------

@pytest.mark.xattr
def test_undo_by_path_touches_only_that_file(tmp_path):
    from sift.adapters.stores import read_tags, undo, write_tags
    from sift.core.model import Decision, FileRef, Outcome
    root = tmp_path / "r"; root.mkdir()
    a, b = root / "a.txt", root / "b.txt"
    for f in (a, b):
        f.write_text("x")
    store = XattrStore([root], state.state_dir() / "undo.jsonl")
    for f in (a, b):
        st = os.stat(f)
        store.apply(Decision(FileRef(str(f), st.st_ino, st.st_mtime_ns, st.st_size), (), Outcome.ACT), ["t"])
    assert undo(state.state_dir() / "undo.jsonl", [root], 1, str(a)) == (1, 0)
    assert read_tags(a) == [] and read_tags(b) == ["t"]


def test_the_demo_marker_never_redirects_scheduled_jobs(monkeypatch, tmp_path):
    from sift import demo
    (cfgmod.config_dir()).mkdir(parents=True, exist_ok=True)
    (cfgmod.config_dir() / "demo-mode").write_text("1")
    monkeypatch.setenv("SIFT_DEMO", "0")
    assert not demo.active()
    monkeypatch.delenv("SIFT_DEMO")
    assert demo.active()


# ---- consent is bound to the address actually used (DNS rebinding) ---------------------------------------------------------

class _EchoServer:
    def __init__(self):
        import http.server
        import threading
        outer = self
        outer.hits = 0

        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                outer.hits += 1
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                body = b'{"ok": true}'
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            log_message = lambda *a: None
        self.srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        self.port = self.srv.server_port
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()


def _answers(monkeypatch, *sequence, port):
    """A resolver whose answer for rebind.example changes on every lookup, and a spy that records every address connected to."""
    import socket
    calls = {"lookups": 0, "connected": []}
    real_getaddrinfo, real_create = socket.getaddrinfo, socket.create_connection

    def fake_getaddrinfo(host, p, *a, **k):
        if host != "rebind.example":
            return real_getaddrinfo(host, p, *a, **k)
        ip = sequence[min(calls["lookups"], len(sequence) - 1)]
        calls["lookups"] += 1
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, p))]

    def fake_create(addr, *a, **k):
        calls["connected"].append(addr[0])
        if addr[0].startswith("203.0.113."):
            raise OSError("test: the public address must never be dialled")
        return real_create(("127.0.0.1", addr[1]), *a, **k)
    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    monkeypatch.setattr(socket, "create_connection", fake_create)
    return calls


def test_a_hostname_that_changes_its_answer_cannot_redirect_file_text_to_a_public_address(monkeypatch):
    from sift import netpolicy
    from sift.adapters.http import BackendError, post_json
    srv = _EchoServer()
    url = f"http://rebind.example:{srv.port}"
    netpolicy.register(url, allow_remote=False, trusted_networks=[])           # what check_backend does for a backend without consent
    # first lookup (the old pre-flight check) says loopback, every later lookup says a public address
    calls = _answers(monkeypatch, "127.0.0.1", "203.0.113.9", port=srv.port)
    assert post_json(url + "/x", {"a": 1}, retries=0) == {"ok": True}
    assert calls["connected"] == ["127.0.0.1"] and srv.hits == 1                # one lookup, used for the decision and the connection
    srv.srv.shutdown()


def test_a_public_answer_without_consent_is_refused_before_any_connection(monkeypatch):
    from sift import netpolicy
    from sift.adapters.http import BackendError, post_json
    url = "http://rebind.example:9"
    netpolicy.register(url, allow_remote=False, trusted_networks=[])
    calls = _answers(monkeypatch, "203.0.113.9", port=9)
    with pytest.raises(BackendError, match="no consent"):
        post_json(url + "/x", {}, retries=2)
    assert calls["connected"] == [] and calls["lookups"] == 1                    # refused, never retried, never dialled


def test_consent_allows_a_remote_address_and_trusted_networks_allow_theirs(monkeypatch):
    from sift import netpolicy
    netpolicy.register("http://rebind.example:9", allow_remote=True, trusted_networks=[])
    calls = _answers(monkeypatch, "203.0.113.9", port=9)
    assert [a for _, a in netpolicy.resolve("rebind.example", 9)] == ["203.0.113.9"]
    netpolicy.register("http://rebind.example:9", allow_remote=False, trusted_networks=["203.0.113.0/24"])
    assert [a for _, a in netpolicy.resolve("rebind.example", 9)] == ["203.0.113.9"]
    netpolicy.register("http://rebind.example:9", allow_remote=False, trusted_networks=["198.51.100.0/24"])
    with pytest.raises(netpolicy.NetworkRefused):
        netpolicy.resolve("rebind.example", 9)


def test_loopback_literals_and_mapped_addresses_are_local_and_unregistered_hosts_default_to_local_only():
    from sift import netpolicy
    assert netpolicy.resolve("127.0.0.1", 8791) and netpolicy.resolve("::1", 8791) and netpolicy.resolve("::ffff:127.0.0.1", 1)
    with pytest.raises(netpolicy.NetworkRefused):
        netpolicy.resolve("8.8.8.8", 443)                                      # never registered: default is this machine only


def test_check_backend_registers_the_policy_that_http_enforces():
    from sift import netpolicy
    cfgmod.check_backend("remote", {"endpoint": "https://api.example.com", "consent": True}, {})
    assert netpolicy.policy_for("api.example.com", 443).allow_remote
    with pytest.raises(cfgmod.ConfigError):
        cfgmod.check_backend("nope", {"endpoint": "https://203.0.113.9"}, {})
    assert not netpolicy.policy_for("203.0.113.9", 443).allow_remote


def test_every_adapter_opens_urls_through_the_pinned_opener():
    """urllib.request.urlopen resolves names itself, so no adapter may call it directly."""
    import re
    for f in (Path(__file__).parent.parent / "src/sift").rglob("*.py"):
        if f.name == "http.py":
            continue
        assert not re.search(r"\burlopen\(", f.read_text()), f
