"""Chat completion for the tuner: an OpenAI-compatible endpoint (Ollama exposes one under /v1). It proposes edits; it never writes anything."""
from __future__ import annotations

from ..usage import estimate_tokens
from .http import BackendError, post_json


class ChatTuner:
    def __init__(self, endpoint: str, model: str, api_key: str | None = None, timeout: float = 240.0, ollama: bool = False):
        if not model:
            raise BackendError("the tuner needs a model name")
        if ollama and (model.endswith(":cloud") or "-cloud" in model):
            raise BackendError(f"refusing model {model!r}: cloud-hosted models would send file excerpts off the machine")
        base = endpoint.rstrip("/")
        self.url = (base + "/v1" if ollama and not base.endswith("/v1") else base) + "/chat/completions"
        self.model, self.timeout = model, timeout
        self.headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self.usage_hook = None

    def complete(self, prompt: str) -> str:
        data = post_json(self.url, {"model": self.model, "temperature": 0.3, "messages": [
            {"role": "system", "content": "You are a careful assistant. Reply with a single JSON object and nothing else."},
            {"role": "user", "content": prompt}]}, self.headers, self.timeout)
        try:
            raw = str(data["choices"][0]["message"]["content"])
        except (KeyError, IndexError, TypeError) as e:
            raise BackendError("the tuner returned an unexpected reply") from e
        if self.usage_hook:
            u = data.get("usage") if isinstance(data.get("usage"), dict) else {}
            if "prompt_tokens" in u:
                self.usage_hook(int(u.get("prompt_tokens") or 0), int(u.get("completion_tokens") or 0), False, u.get("cost"))
            else:
                self.usage_hook(estimate_tokens(len(prompt)), estimate_tokens(len(raw)), True, None)
        return raw
