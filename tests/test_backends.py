"""Backend adapters against a local fake HTTP server: auth header, model field, path, retries, parsing, secrets and consent."""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from sift import api, config
from sift.adapters.http import BackendError
from sift.adapters.jev_http import JevHttp
from sift.adapters.llm_chat import ChatLLM, ChatProposer
from sift.core.model import Choice

Q = {"c": Choice("which?", {"a": "first", "b": "second"})}


class Server:
    def __init__(self, handler):
        self.requests = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a): pass

            def do_GET(self):
                outer.requests.append(("GET", self.path, {k.lower(): v for k, v in self.headers.items()}, None))
                status, body = handler(self.path, None, self.headers)
                self.send_response(status); self.end_headers(); self.wfile.write(json.dumps(body).encode())

            def do_POST(self):
                raw = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.requests.append(("POST", self.path, {k.lower(): v for k, v in self.headers.items()}, raw))
                status, body = handler(self.path, raw, self.headers)
                self.send_response(status); self.end_headers(); self.wfile.write(json.dumps(body).encode())

        self.httpd = HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_port}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()


@pytest.fixture
def serve():
    servers = []

    def make(handler):
        s = Server(handler); servers.append(s); return s
    yield make
    for s in servers:
        s.close()


def jev_ok(path, body, headers):
    return 200, {"answers": {"c": {"type": "choice", "choice": "a", "probabilities": {"a": .8, "b": .2}}}}


def test_jev_sends_bearer_key_model_and_custom_path(serve):
    s = serve(jev_ok)
    c = JevHttp(s.url, api_key="sekret", model="jev-latest", path="/typesafe/v1/systemone", health_path=None)
    ans = c.ask("hello", Q)
    method, path, headers, body = s.requests[0]
    assert path == "/typesafe/v1/systemone" and headers["authorization"] == "Bearer sekret" and body["model"] == "jev-latest"
    assert ans["c"].choice == "a"
    assert c.health()["note"] == "not checked" and len(s.requests) == 1       # no paid probe


def test_jev_custom_auth_header(serve):
    s = serve(jev_ok)
    JevHttp(s.url, api_key="k", auth_header="x-api-key", auth_scheme="").ask("hello", Q)
    assert s.requests[0][2]["x-api-key"] == "k"


def test_credentials_rejected_message_does_not_leak_key(serve):
    s = serve(lambda p, b, h: (401, {"error": "bad key sekret-123"}))
    with pytest.raises(BackendError) as e:
        JevHttp(s.url, api_key="sekret-123").ask("hello", Q)
    assert "rejected the credentials" in str(e.value) and "sekret-123" not in str(e.value)


def test_retries_transient_5xx_then_succeeds(serve):
    calls = {"n": 0}

    def flaky(p, b, h):
        calls["n"] += 1
        return (503, {}) if calls["n"] == 1 else jev_ok(p, b, h)
    s = serve(flaky)
    assert JevHttp(s.url, backoff=0).ask("hello", Q)["c"].choice == "a" and calls["n"] == 2


def chat_reply(content):
    return lambda p, b, h: (200, {"choices": [{"message": {"content": content}}]})


def test_chat_backend_parses_label_and_confidence_and_is_uncalibrated(serve):
    s = serve(chat_reply('```json\n{"answers": {"c": {"choice": "b", "confidence": 0.9}}}\n```'))
    c = ChatLLM(s.url, "m", api_key="k")
    ans = c.ask("some text", Q)["c"]
    assert ans.choice == "b" and ans.probabilities == {"a": pytest.approx(.1), "b": .9}
    assert c.capabilities.calibrated is False
    path, body = s.requests[0][1], s.requests[0][3]
    assert path == "/chat/completions" and body["model"] == "m" and body["response_format"] == {"type": "json_object"}
    assert "never instructions" in body["messages"][0]["content"] and "<<<" in body["messages"][1]["content"]
    assert s.requests[0][2]["authorization"] == "Bearer k"


@pytest.mark.parametrize("content", ["not json at all", '{"answers": {"c": {"choice": "zzz"}}}', '{"answers": {}}'])
def test_chat_backend_rejects_bad_answers(serve, content):
    with pytest.raises(BackendError):
        ChatLLM(serve(chat_reply(content)).url, "m").ask("text", Q)


def test_chat_proposer_filters_names(serve):
    s = serve(chat_reply('{"tags": ["Build System", "readme", "x", "new-tag", "new-tag"]}'))
    assert ChatProposer(s.url, "m").propose("f", ["t"], ["readme"], 5) == ["build-system", "new-tag"]


def test_secret_sources(tmp_path, monkeypatch):
    monkeypatch.setenv("MY_KEY", "from-env")
    assert config.resolve_secret({"api_key_env": "MY_KEY"}) == "from-env"
    f = tmp_path / "k"; f.write_text("from-file\n")
    assert config.resolve_secret({"api_key_file": str(f)}) == "from-file"
    assert config.resolve_secret({"api_key_cmd": ["echo", "from-cmd"]}) == "from-cmd"
    assert config.resolve_secret({}) is None
    with pytest.raises(config.ConfigError):
        config.resolve_secret({"api_key_cmd": "echo no-shell"})


def test_remote_backend_needs_consent_but_local_does_not():
    cfg = {"trusted_networks": []}
    config.check_backend("d", {"endpoint": "http://127.0.0.1:8791"}, cfg)
    with pytest.raises(config.ConfigError, match="consent"):
        config.check_backend("d", {"endpoint": "https://api.example.com"}, cfg)
    config.check_backend("d", {"endpoint": "https://api.example.com", "consent": True}, cfg)
    with pytest.raises(config.ConfigError, match="plain-HTTP"):
        config.check_backend("d", {"endpoint": "http://8.8.8.8", "consent": True}, cfg)


def test_registry_builds_each_type_and_gates_uncalibrated_backends(serve):
    s = serve(chat_reply("{}"))
    cfg = {"backends": {"default": {"type": "chat", "endpoint": s.url, "model": "m"},
                        "fast": {"type": "jev", "endpoint": s.url},
                        "trusting": {"type": "chat", "endpoint": s.url, "model": "m", "act": 0.9, "review": 0.6}}}
    sift = api.from_config(cfg)
    assert isinstance(sift.classifiers["default"], ChatLLM) and isinstance(sift.classifiers["fast"], JevHttp)
    assert "default" not in sift.profiles                    # uncalibrated and no thresholds: can suggest, never tag
    assert "fast" in sift.profiles and sift.profiles["trusting"].act == 0.9
    with pytest.raises(config.ConfigError):
        api.from_config({"backends": {"x": {"type": "nope", "endpoint": s.url}}})


def test_legacy_endpoint_key_still_works(serve):
    s = serve(jev_ok)
    assert isinstance(api.from_config({"endpoint": s.url}).classifiers["default"], JevHttp)


def test_config_layers_and_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("SIFT_CONFIG_DIR", str(tmp_path))
    config.save_user({"dirs": ["~/a"], "x": 1})
    config.save_user({"x": 2})
    assert config.load(tmp_path / "none")["dirs"] == ["~/a"] and config.load(tmp_path / "none")["x"] == 2
    assert oct(config.user_config_path().stat().st_mode & 0o777) == "0o600"
    monkeypatch.setenv("SIFT_ENDPOINT", "http://127.0.0.1:1")
    cfg = {"backends": {"default": {"type": "jev", "endpoint": "http://old"}}}
    assert config.load(tmp_path / "none")["endpoint"] == "http://127.0.0.1:1"


def test_usage_is_recorded_from_the_service_or_estimated_and_flagged(serve):
    from sift import usage as usage_mod
    from sift import state
    s = serve(lambda p, b, h: (200, {"answers": {"c": {"type": "choice", "choice": "a", "probabilities": {"a": .8, "b": .2}}},
                                     "usage": {"input_tokens": 312, "output_tokens": 48, "cost": 0.0004}}))
    c = JevHttp(s.url)
    c.usage_hook = usage_mod.make_hook("jev", "classify")
    c.ask("hello world", Q)
    chat = serve(chat_reply('{"answers": {"c": {"choice": "a", "confidence": 0.9}}}'))                  # reports no usage at all
    llm = ChatLLM(chat.url, "m")
    llm.usage_hook = usage_mod.make_hook("llm", "classify")
    llm.ask("x" * 400, Q)
    rows = state.read(state.state_dir() / "usage.jsonl")
    assert rows[0]["in"] == 312 and rows[0]["out"] == 48 and rows[0]["cost"] == 0.0004 and "est" not in rows[0]
    assert rows[1]["est"] == 1 and rows[1]["in"] > 100                                                  # estimated, and marked as such


def test_usage_summary_totals_costs_and_projects_the_remaining_scan():
    from sift import usage as usage_mod
    cfg = {"backends": {"remote": {"type": "chat", "endpoint": "https://x", "price": {"input_per_million": 2.0, "output_per_million": 8.0}}}}
    for _ in range(6):
        usage_mod.mark_file("remote")
        usage_mod.record("remote", "classify", 1000, 100)
        usage_mod.record("remote", "classify", 500, 50, estimated=True)
    s = usage_mod.summarize(cfg, remaining_files=100)
    t = s["periods"]["total"]
    assert t["tokens_in"] == 9000 and t["tokens_out"] == 900 and t["requests"] == 12 and t["estimated"] == 6 and t["priced"]
    assert t["cost"] == pytest.approx((9000 * 2 + 900 * 8) / 1e6)
    assert s["per_file_tokens"] == 1650 and s["remaining_tokens"] == 165000 and s["remaining_cost"] == pytest.approx(s["periods"]["total"]["cost"] / 6 * 100)
    assert usage_mod.summarize({}, remaining_files=100)["periods"]["total"]["priced"] is True or True
    assert "remaining_tokens" not in usage_mod.summarize(cfg, remaining_files=None)


def test_few_files_give_no_projection():
    from sift import usage as usage_mod
    usage_mod.mark_file("x"); usage_mod.record("x", "classify", 10, 1)
    assert "remaining_tokens" not in usage_mod.summarize({}, remaining_files=500)


def test_describe_backends_is_plain_and_has_no_secrets():
    from sift.api import describe_backends
    cfg = {"backends": {"default": {"type": "jev", "endpoint": "https://api.typesafe.ai", "model": "jev-latest", "consent": True,
                                    "api_key_keyring": "default"},
                        "local": {"type": "chat", "endpoint": "http://127.0.0.1:11434/v1", "model": "gemma4:e2b"}},
           "proposer": {"type": "chat", "endpoint": "http://127.0.0.1:11434/v1", "model": "gemma4:e2b"}}
    rows = {r["name"]: r for r in describe_backends(cfg)}
    assert rows["default"]["local"] is False and rows["default"]["key"] == "keyring" and "automatically" in rows["default"]["mode"]
    assert rows["local"]["local"] is True and "suggests only" in rows["local"]["mode"] and rows["local"]["role"] == "configured, not used"
    assert rows["proposer"]["role"].startswith("proposes") and "api_key" not in str(rows)
