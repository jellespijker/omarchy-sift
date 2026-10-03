# ADR-0005: Descriptive tags come from a user vocabulary scored per chunk

Status: Accepted

## Context
The decision tree (ADR-0001, ADR-0003) answers "what kind of file is this" with a small closed set (`document`, `code`, `log`, `data`, `other`). On a real downloads folder this produced generic tags. Two Dutch speech-to-text transcripts of job interviews were only labeled `document`, at 0.59 and 0.77 root confidence, and went to review.

Measurements on those two files and four controls (Open-Jev, yes/no per tag):
- `transcript` scored 0.72 and 0.87 on the first 2,000 characters. Controls stayed at or below 0.06.
- `dutch` scored 0.96 and 0.97.
- `job-hunt` scored 0.04 on the first 2,000 characters, because the opening is introductions. Over five chunks the best chunk scored 0.52 and 0.67. An unrelated control stayed at 0.14 or less and a CV-like control scored 0.41.

## Decision
- A user-editable **vocabulary** of descriptive tags (name plus a one-sentence description) sits next to the path tree. Defaults live in `tree.json`; personal additions go in the untracked `config.json` through `sift vocab add`.
- Each tag is a yes/no question. Text is split into up to 3 evenly spaced chunks, all tags are asked per chunk in one batched request, and the tag's score is its best chunk.
- A score at or above the tag's `act` threshold (default: the backend profile) is written. A score at or above `review` is only suggested and appears in the review panel. Tags can carry their own `act` threshold.
- Vocabulary scoring needs at least 200 characters of text. It is skipped when the path confidently says `data` or `log` (`vocabulary_skip`), because those are mostly measurement dumps and error stubs, and the cost is high.
- The path tags stay, but `other` is never written as a tag, and an incomplete path (for example `document` without its subtype) is not suggested.
- **Proposing new tags:** `sift propose PATH` asks a configured Ollama model for up to 5 candidate tag names. It only prints suggestions. Adding one is an explicit `sift vocab add`. The adapter refuses model names ending in `:cloud`, so file content cannot leave the local network through it, and it sends `keep_alive: 0` so the model unloads and does not compete with the classifier for GPU memory.

## Consequences
- Tags become specific (`transcript`, `job-hunt`, `3d-printing`) and the vocabulary grows with use.
- Cost: long text files take several requests. The server serializes requests and one 8-question request on a 1,500-character chunk took roughly 6-13 seconds under load, so a first full scan of a large folder takes hours. Scans therefore run newest-first.
- Scores are best-chunk maxima, so an unrelated passage can trigger a tag. Per-tag thresholds exist for that, but there is no real-file accuracy yet (see `docs/EVAL.md`).
- Open-Jev gave a GPU out-of-memory error once while a 27B model was also loaded by the proposer. The client retries transient 5xx responses.

## Alternatives rejected
- **Free-text tags from the classifier:** it only picks from given labels.
- **One chunk only:** `job-hunt` was invisible in the first chunk.
- **Let the LLM write tags directly to files:** unreviewed, unbounded vocabulary and a larger injection surface. Proposals stay suggestions.
