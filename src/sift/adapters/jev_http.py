"""Classifier adapter for the Jev "system one" decision API (POST /v1/systemone).

Works with a self-hosted Open-Jev server, the hosted TypeSafe API and gateways that pass the same body through (LiteLLM,
OpenRouter's compatible routes): the differences are only the base URL, `path`, an optional `model` and the auth header.
"""
from __future__ import annotations

from typing import Mapping

from ..core.model import Answer, Capabilities, Choice, Question
from ..usage import estimate_tokens
from .http import BackendError, get_json, post_json

__all__ = ["JevHttp", "BackendError"]


class JevHttp:
    capabilities = Capabilities(frozenset({"text"}), frozenset({"choice", "multi"}), max_labels=20, max_chars=2000, calibrated=True)

    def __init__(self, endpoint: str, timeout: float = 30.0, retries: int = 2, backoff: float = 1.0, api_key: str | None = None,
                 model: str | None = None, path: str = "/v1/systemone", health_path: str | None = "/health",
                 auth_header: str = "Authorization", auth_scheme: str = "Bearer"):
        self.endpoint = endpoint.rstrip("/")
        self.timeout, self.retries, self.backoff = timeout, retries, backoff
        self.model, self.path, self.health_path = model, "/" + path.lstrip("/"), health_path
        self.headers = {auth_header: f"{auth_scheme} {api_key}".strip()} if api_key else {}
        self.usage_hook = None

    def _post(self, body: dict) -> dict:
        if self.model:
            body = {**body, "model": self.model}
        return post_json(self.endpoint + self.path, body, self.headers, self.timeout, self.retries, self.backoff)

    def health(self) -> dict:
        if not self.health_path:                       # hosted services often have none, and probing would cost money
            return {"status": "ready", "model": self.model or "", "note": "not checked"}
        return get_json(self.endpoint + self.health_path, self.headers)

    def ask(self, text: str, questions: Mapping[str, Question]) -> Mapping[str, Answer]:
        if not text.strip():
            raise BackendError("empty text")
        qs = {}
        for name, q in questions.items():
            if not isinstance(q, Choice):
                raise BackendError(f"unsupported question kind for {name}")
            qs[name] = {"type": "choice", "instructions": q.instructions, "criteria": dict(q.labels)}
        state = text
        for _ in range(4):  # on over-length 422, halve and retry
            try:
                data = self._post({"state": state, "questions": qs})
                break
            except BackendError as e:
                if "HTTP 422" in str(e) and len(state) > 200:
                    state = state[: len(state) // 2]
                    continue
                raise
        else:
            raise BackendError("input too long")
        self._report(data, len(state))
        return self._parse(data, questions)

    def _report(self, data: dict, chars_sent: int) -> None:
        if not self.usage_hook:
            return
        u = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        if "input_tokens" in u:
            self.usage_hook(int(u.get("input_tokens") or 0), int(u.get("output_tokens") or 0), False, u.get("cost"))
        else:
            self.usage_hook(estimate_tokens(chars_sent), 16, True, None)

    @staticmethod
    def _parse(data: dict, questions: Mapping[str, Question]) -> dict[str, Answer]:
        answers = data.get("answers")
        if not isinstance(answers, dict) or set(answers) != set(questions):
            raise BackendError("response does not match the questions asked")
        out: dict[str, Answer] = {}
        for name, q in questions.items():
            a = answers[name]
            probs = a.get("probabilities")
            labels = list(q.labels)  # type: ignore[union-attr]
            if (a.get("type") != "choice" or not isinstance(probs, dict) or set(probs) != set(labels)
                    or a.get("choice") not in labels):
                raise BackendError(f"invalid answer for {name}")
            vals = list(probs.values())
            if any(not isinstance(p, (int, float)) or p < 0 or p > 1 for p in vals) or abs(sum(vals) - 1) > 1e-6:
                raise BackendError(f"invalid probabilities for {name}")
            out[name] = Answer(a["choice"], {k: float(v) for k, v in probs.items()})
        return out
