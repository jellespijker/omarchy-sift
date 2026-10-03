# ADR-0012: Escalating suggestions and tuning tags from your own verdicts

Status: Accepted

## Context
About 972 files carried suggestions (0.4–0.8, or a keyword-gate miss) that no one would ever review one by one, and tag settings (descriptions,
keyword gates, thresholds) were tuned by hand. A free-form ReAct agent per file was rejected: the classifier server handles about 1.7 requests/s,
weak models fail tool loops, and giving tools to a model that reads untrusted file text breaks the closed-action-set rule (ADR-0002, SPEC security).

## Decision
**Escalation.** `escalate: {backend: NAME}` names a second, stronger backend. When the first pass leaves vocabulary suggestions, the stronger
backend is asked only about those tags (keyword gate not re-applied: it is the judge). Tags it confirms at its own act threshold are written
(`reason: "confirmed by NAME"`); a backend error keeps the first-pass result. The backend must be trusted: calibrated or with explicit
`act`/`review` thresholds. `sift escalate set|off|status` refuses an uncalibrated chat backend. Consent and transport rules are unchanged.

**Tuning.** `sift tune run [TAG]` takes a tag with at least 6 verdicts (`sift audit`: ok, wrong, missed) and asks a model (`tuner`, else `proposer`;
Ollama or any OpenAI-compatible chat endpoint) for up to 3 edits. The edit set is closed: that tag's description, `require`, `require_min`, `act`.
Anything else, other tags, invalid values and nested-quantifier regexes are dropped. Each edit is replayed on the judged files; it is kept only if
precision does not drop, recall does not drop, and one rises (`core/tune.better`; predicting nothing never wins). `sift tune list` shows proposals
and `sift tune apply TAG` writes the merged entry into the user vocabulary. Nothing is applied automatically; no file or tag is touched by tuning.

**Data sent.** Up to 3 + 3 excerpts of 300 characters per tag go to the tuner, never from files flagged as secrets, and only to a backend that passed
the same consent check as classification. Excerpts are wrapped as data in the prompt.

## Consequences
- Tuning is only as good as the verdicts; with few verdicts it does nothing (it says so).
- Escalation spends the stronger backend's tokens on suggested tags only; usage is recorded under its name.
- Replay uses current classifier scores, so a tune result is valid for the backend it was measured on.
- Not built: tuning of prompts, profiles or new tags (use `sift discover`), automatic periodic tuning, an agent CLI such as `agy` as the tuner.
- Guardrails: `tests/test_tune.py` (closed set, replay gate, escalation promotion and failure, CLI refusals).
