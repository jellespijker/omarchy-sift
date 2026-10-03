# ADR-0003: Tag-only v1, confidence gating, eval first

Status: Accepted

## Context
Three independent red-team reviews (security, classifier validity, product) of the first spec agreed on the following. Measurements on small synthetic samples (indicative, to be re-verified on real files):
- Confidence at or above 0.8 was reliable; 0.4-0.8 was about a coin flip.
- Accuracy falls as the label count grows. Filename-only evidence produced confident errors.
- The server serializes requests, so parallel workers do not help.
- Input over the token limit fails with HTTP 422, with no silent truncation.

Product and security concerns: a review queue nobody processes, dry-run-by-default that never ships value, destructive file moves as the riskiest code, and prompt injection through file content.

## Decision
- v1 only tags files. It does not move, delete, or overwrite anything.
- A node acts only at or above its backend's calibrated "act" level. The middle band goes to review. Filename-only evidence never triggers an action.
- The first deliverable after the client and runner is `sift eval`: per-node accuracy, a calibration table, and an injection corpus, run against about 100 hand-labeled real files. The eval decides whether the product is viable and what the thresholds are.
- `sift scan <dir>` writes a report first. The xattr store is added only after the eval passes.
- A single worker with early-exit rules and folder-level sampling replaces parallelism.
- Deferred until the eval justifies them: watcher, review panel, undo log, bar widget, screen triage, file moves, duplicate and secret audits, generative tag suggestions, image backends.

## Consequences
- The first slice is small and testable without touching the Quickshell shell or running a daemon.
- The eval may show the classifier is not good enough. That outcome is acceptable and cheap to learn early (see the exit strategy in ADR-0001).
- Trust-building path: the report shows what would be tagged, with measured agreement, before any write is enabled.

## Alternatives rejected
- **Build all features first, evaluate later:** repeats the scope risk all three reviews flagged.
- **Move files from day one:** the most dangerous code, with tags offering most of the value.
