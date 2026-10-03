"""The public interface: classify(path) and apply(decision, store). Wiring only."""
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping

from . import config as cfgmod
from . import validate as _validate          # noqa: F401  (connects the pure validators to the user's language)
from . import usage
from .adapters.extract import FileExtractor
from .adapters.jev_http import JevHttp
from .core import profiles as prof
from .core.model import Decision, Outcome, Profile
from .core.runner import Classifier, run, sanitize_tag, score_vocabulary
from .core.tree import Tree, load_tree
from .ports import Extractor, TagStore


class Sift:
    def __init__(self, tree: Tree, classifiers: Mapping[str, Classifier], extractor: Extractor,
                 profiles: Mapping[str, Profile] | None = None):
        self.tree, self.classifiers, self.extractor = tree, classifiers, extractor
        self.profiles = dict(profiles or {})
        self._derived: dict[str, Tree] = {}

    escalator: Classifier | None = None      # a stronger backend asked about tags the first pass could only suggest
    escalator_profile: Profile = Profile()
    escalator_name = ""
    on_file = None          # optional callback after each classified file (used for per-file token averages)

    def classify(self, path: Path) -> Decision:
        p = self.profile_for(path)
        if p and p.skip:
            return Decision(self.extractor.extract(path).ref, (), Outcome.UNDECIDED, reason=f"skipped by profile {p.name!r}")
        tree = self.tree_for(p)
        d = run(self.extractor.extract(path), self.classifiers, tree, self.profiles)
        if self.escalator and d.suggested:
            d = self.escalate(path, d, tree)
        if self.on_file:
            self.on_file()
        return d

    def reload(self, cfg: Mapping[str, Any], tree_path: Path | None = None) -> None:
        """Pick up tag renames, merges, deletions and prompt changes made while a long run is in progress."""
        self.tree = load_tree_for(cfg, tree_path)
        self._derived = {}

    def escalate(self, path: Path, d: Decision, tree: Tree) -> Decision:
        """Ask the stronger backend about suggested vocabulary tags. Tags it confirms at its own act threshold are promoted and written;
        the keyword gate is not re-applied because the stronger model is the judge here. Any backend failure keeps the first-pass result."""
        from .adapters.http import BackendError
        cand = [t for t in d.suggested if t in tree.vocabulary and not tree.vocabulary[t].detector]
        if not cand:
            return d
        sub = replace(tree, vocabulary={t: replace(tree.vocabulary[t], require=None) for t in cand})
        try:
            scores = score_vocabulary(self.extractor.extract(path), self.escalator, sub, self.escalator_profile)
        except (BackendError, OSError):
            return d
        ok = [t for t in cand if scores.get(t, 0.0) >= (tree.vocabulary[t].act if tree.vocabulary[t].act is not None else self.escalator_profile.act)]
        if not ok:
            return d
        promoted = tuple(t for t in dict.fromkeys(tree.tag_map.get(sanitize_tag(t), sanitize_tag(t)) for t in ok) if t)
        rest = tuple(t for t in d.suggested if t not in ok and t not in promoted)
        return replace(d, outcome=Outcome.ACT, tags=tuple(dict.fromkeys([*d.tags, *promoted])), suggested=rest,
                       reason=f"confirmed by {self.escalator_name}: " + ", ".join(ok), scores={**d.scores, **{t: scores[t] for t in ok}})

    def profile_for(self, path: Path) -> prof.Profile | None:
        return prof.select(self.tree.profiles, str(path))

    def tree_for(self, p: prof.Profile | None) -> Tree:
        if p is None:
            return self.tree
        if p.name not in self._derived:
            self._derived[p.name] = prof.derive(self.tree, p)
        return self._derived[p.name]

    def apply(self, decision: Decision, store: TagStore) -> str:
        if decision.outcome is not Outcome.ACT or not decision.tags:
            return "skipped"
        return store.apply(decision, decision.tags)


def load_tree_for(cfg: Mapping[str, Any], tree_path: Path | None = None) -> Tree:
    """The decision tree plus the user's own vocabulary, tag renames and merges."""
    data = json.loads((tree_path or cfgmod.ROOT / "tree.json").read_text())
    pr, node_pr, pr_problems = prof.parse_prompts(cfg.get("prompts"), "prompts")
    for nid, text in node_pr.items():
        if nid in data.get("nodes", {}):
            data["nodes"][nid]["instructions"] = text
    tree = load_tree(data, cfg.get("vocabulary"), cfg.get("tag_map"), pr)
    profiles, problems = prof.load_profiles(data.get("profiles", []), [_expand(x) for x in cfg.get("profiles") or []])
    tree = replace(tree, profiles=profiles, problems=tree.problems + tuple(pr_problems) + tuple(problems))
    disabled = set(cfg.get("disabled_tags") or [])
    if disabled & set(tree.vocabulary):
        tree = replace(tree, vocabulary={t: d for t, d in tree.vocabulary.items() if t not in disabled})
    return tree


def _expand(raw: Any) -> Any:
    """Expand "~" in a profile's directories (the core never touches the file system or environment)."""
    if isinstance(raw, dict) and isinstance(raw.get("match"), dict):
        m = dict(raw["match"])
        m["dirs"] = [str(Path(str(d)).expanduser()) for d in m.get("dirs", [])]
        return {**raw, "match": m}
    return raw


def make_classifier(name: str, b: Mapping[str, Any], cfg: Mapping[str, Any]):
    """Instantiate one backend from its config entry, after the transport and consent checks."""
    cfgmod.check_backend(name, dict(b), dict(cfg))
    key = None if b.get("type") == "demo" else cfgmod.resolve_secret(dict(b))
    clf = _build_classifier(b, cfg, key)
    clf.usage_hook = usage.make_hook(name, "classify")
    return clf


def _build_classifier(b: Mapping[str, Any], cfg: Mapping[str, Any], key: str | None):
    from .adapters.llm_chat import ChatLLM
    if b.get("type") == "demo":
        from .adapters.demo import DemoClassifier
        return DemoClassifier()
    if b.get("type", "jev") == "chat":
        return ChatLLM(b["endpoint"], b.get("model", ""), key, timeout=float(b.get("timeout", 120)), json_mode=b.get("json_mode", True),
                       max_chars=b.get("max_chars"), system=prof.parse_prompts(cfg.get("prompts"), "prompts")[0].get("chat_system"))
    return JevHttp(b["endpoint"], timeout=float(b.get("timeout", 30)), api_key=key, model=b.get("model"), path=b.get("path", "/v1/systemone"),
                   health_path=b.get("health_path", "/health" if cfgmod.is_local(b["endpoint"], list(cfg.get("trusted_networks", []))) else None),
                   auth_header=b.get("auth_header", "Authorization"), auth_scheme=b.get("auth_scheme", "Bearer"))


def make_proposer(cfg: Mapping[str, Any], keep_alive: str | int = 0):
    """The tag-name proposer (Ollama native API or any OpenAI-compatible chat endpoint), or None when not configured."""
    from .adapters.llm_chat import ChatProposer
    from .adapters.ollama import OllamaProposer
    pc = cfg.get("proposer") or {}
    if not pc.get("endpoint") or not pc.get("model"):
        return None
    cfgmod.check_backend("proposer", pc, dict(cfg))
    key = cfgmod.resolve_secret(pc)
    tpl = prof.parse_prompts(cfg.get("prompts"), "prompts")[0].get("propose")
    prop = ChatProposer(pc["endpoint"], pc["model"], key, template=tpl) if pc.get("type", "ollama") == "chat" else \
        OllamaProposer(pc["endpoint"], pc["model"], timeout=240, keep_alive=keep_alive, template=tpl)
    prop.usage_hook = usage.make_hook("proposer", "propose")
    return prop


def make_tuner(cfg: Mapping[str, Any]):
    """The model that proposes tag edits: `tuner` in the config, else the tag proposer. None when neither is set."""
    from .adapters.llm_tuner import ChatTuner
    tc = cfg.get("tuner") or cfg.get("proposer") or {}
    if not tc.get("endpoint") or not tc.get("model"):
        return None
    cfgmod.check_backend("tuner", tc, dict(cfg))
    tuner = ChatTuner(tc["endpoint"], tc["model"], cfgmod.resolve_secret(tc), ollama=tc.get("type", "ollama") == "ollama")
    tuner.usage_hook = usage.make_hook("tuner", "tune")
    return tuner


def from_config(cfg: Mapping[str, Any], tree_path: Path | None = None) -> Sift:
    table = cfgmod.backends(dict(cfg))
    if "default" not in table:
        raise cfgmod.ConfigError("no backend configured: run `sift setup` or add one under \"backends\" in the config")
    classifiers = {name: make_classifier(name, b, cfg) for name, b in table.items()}
    profiles: dict[str, Profile] = {}
    for name, b in table.items():
        if "act" in b or "review" in b:            # explicit thresholds are the user's decision to trust this backend
            profiles[name] = Profile(float(b.get("act", 0.8)), float(b.get("review", 0.4)))
        elif classifiers[name].capabilities.calibrated:
            profiles[name] = Profile(cfg.get("threshold_act", 0.8), cfg.get("threshold_review", 0.4))
    sift = Sift(load_tree_for(cfg, tree_path), classifiers, FileExtractor(), profiles)
    sift.on_file = lambda: usage.mark_file("default")
    esc = (cfg.get("escalate") or {}).get("backend")
    if esc and esc != "default" and esc in classifiers and esc in profiles:          # only a trusted (calibrated or explicit-threshold) backend
        sift.escalator, sift.escalator_profile, sift.escalator_name = classifiers[esc], profiles[esc], esc
    return sift


def describe_backends(cfg: Mapping[str, Any], default_model: str = "") -> list[dict[str, Any]]:
    """What is being used, in plain terms, for the panel and `sift backends`: no keys, no URLs with credentials."""
    from urllib.parse import urlparse
    table = cfgmod.backends(dict(cfg))
    nets = list(cfg.get("trusted_networks", []))
    tree_backends = {n.backend for n in load_tree_for(cfg).nodes.values()} - {"default"}
    out: list[dict[str, Any]] = []
    for name, b in table.items():
        host = urlparse(b["endpoint"]).hostname or b["endpoint"]
        calibrated = b.get("type", "jev") in ("jev", "demo")
        auto = calibrated or "act" in b
        role_code = "classifies" if name == "default" else ("tree" if name in tree_backends else "unused")
        out.append({"role_code": role_code, "mode_code": "auto" if auto else "suggest", "key_code": cfgmod.key_code(b), "name": name, "type": b.get("type", "jev"), "model": b.get("model") or (default_model if name == "default" else ""),
                    "host": host, "local": cfgmod.is_local(b["endpoint"], nets), "key": cfgmod.key_source(b),
                    "mode": "tags automatically at high confidence" if auto else "suggests only; you accept each tag",
                    "role": "classifies files and scores your tags" if name == "default" else
                            ("used by some tree questions" if name in tree_backends else "configured, not used")})
    pc = cfg.get("proposer") or {}
    if pc.get("endpoint") and pc.get("model"):
        out.append({"name": "proposer", "type": pc.get("type", "ollama"), "model": pc["model"], "host": urlparse(pc["endpoint"]).hostname or "",
                    "local": cfgmod.is_local(pc["endpoint"], nets), "key": cfgmod.key_source(pc), "mode": "suggests new tag names only",
                    "role_code": "proposes", "mode_code": "propose", "key_code": cfgmod.key_code(pc),
                    "role": "proposes tag names (never writes tags)"})
    return out
