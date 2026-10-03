"""An offline stand-in classifier for demo mode: deterministic, keyword based, no network."""
from __future__ import annotations

import hashlib
from typing import Mapping

from ..core.model import Answer, Capabilities, Choice, Question

_HINTS = {"document": ("meeting", "invoice", "agreement", "notes", "letter", "interview", "datasheet", "minutes", "tax", "policy"),
          "code": ("def ", "#include", "cmake", "import ", "function"), "log": ("error", "warn", "traceback", "generic log"),
          "data": ("t,", ",temp", "0,2", "measurements")}


class DemoClassifier:
    capabilities = Capabilities(frozenset({"text"}), frozenset({"choice", "multi"}), 40, 2000, True)
    usage_hook = None

    def health(self) -> dict:
        return {"status": "ready", "model": "demo-classifier", "note": "offline demo"}

    def ask(self, text: str, questions: Mapping[str, Question]) -> Mapping[str, Answer]:
        low = text.lower()
        out: dict[str, Answer] = {}
        for qid, q in questions.items():
            labels = list(q.labels)  # type: ignore[union-attr]
            if set(labels) == {"yes", "no"}:                                   # a vocabulary tag question
                tag = q.instructions.split("'")[1] if "'" in q.instructions else ""
                words = [w for w in tag.replace("-", " ").split() if len(w) > 2]
                hits = sum(low.count(w) for w in words)
                p = min(0.15 + 0.22 * hits, 0.95)
                out[qid] = Answer("yes" if p >= 0.5 else "no", {"yes": p, "no": 1 - p})
                continue
            scores = {lab: sum(low.count(h) for h in _HINTS.get(lab, (lab,))) + (0.1 if lab == "other" else 0) for lab in labels}
            best = max(scores, key=lambda k: (scores[k], k))
            jitter = int(hashlib.sha1(text.encode()).hexdigest()[:2], 16) / 255 * 0.1
            top = min(0.55 + 0.1 * scores[best] + jitter, 0.93)
            rest = (1 - top) / (len(labels) - 1) if len(labels) > 1 else 0
            out[qid] = Answer(best, {lab: (top if lab == best else rest) for lab in labels})
        if self.usage_hook:
            self.usage_hook(max(len(text) // 4, 1), 12, True, None)
        return out
