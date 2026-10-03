# Sift spec (revised after red-team, architecture review and productisation)

Decisions behind this spec are recorded in [docs/adr/](adr/). Where the two disagree, the ADR wins until the spec is updated.

> Product configuration, pluggable backends (Jev and chat), the tag manager and the tray rules are specified in
> [ADR-0007](adr/0007-product-configuration-and-backends.md) and the README, which take precedence over older sections below.

## Principles
- Tag only in v1. No moves, deletes, or overwrites.
- Low confidence means a human decides.
- Every decision stores its path and per-node probabilities. No file content in logs or DB.
- File content goes only to the configured backend (default: Open-Jev on localhost; a remote server is set per user in the untracked `config.json` or `SIFT_ENDPOINT`).
- The core is pure. It performs no I/O and has no side effects (ADR-0001).
- No backend, modality, or question kind is hardcoded into the core. Backends declare capabilities (ADR-0002).

## Architecture

One deep module with a two-call interface, a pure core, and three ports.

```
src/sift/
  core/        model.py  tree.py  policy.py  runner.py      # pure: no I/O
  ports.py     Classifier, Extractor, TagStore (typing.Protocol)
  adapters/    jev_http.py  extract.py  xattr_store.py  report_store.py
  config.py    env > config.json > config.json.example defaults
  cli.py       `sift scan | eval`, `--json`; wiring only
```

Interface (the test surface):

```python
decision = sift.classify(path)    # -> Decision(ref, steps, outcome, tags)
sift.apply(decision, store)       # writes tags only when outcome == ACT
```

The CLI, a later watcher, and a later Quickshell panel all use these two calls. The panel shells out to `sift scan --json`, like other Omarchy plugins that keep their logic in a `bin/` script.

### Ports
- `Classifier`: answers a set of questions about some evidence. Has a real HTTP adapter and a deterministic fake for tests and eval replay.
- `Extractor`: turns a file into evidence parts, with size caps and sandboxed external tools. This is where hostile-input handling lives.
- `TagStore`: the only code that writes. Has a report-only adapter (default) and an xattr adapter.

### Evidence and questions (ADR-0002)
- `Evidence` is a bundle of parts: `{text?, image?, filename, mime, metadata}`. v1 implements text and filename only.
- Questions are a tagged union: `Choice(labels)` and `Multi(tags)` are implemented. `Score` and `Extract` are reserved names, not code.
- A node declares which question kind it asks.

### Backend capabilities
Each backend declares `{modalities, kinds, max_labels, max_chars, calibrated}`. The runner reads these for truncation, label-count checks, and routing, rather than hardcoding Open-Jev limits.

- Thresholds belong to a per-backend profile, not to the tree. A node asks for a confidence level (for example "act" or "review"). The profile maps that to a number for each backend, produced by `sift eval`'s calibration table.
- Backends are registered by name in `config.json` (`backends: {name: {...}}`). A node or modality selects one, and a router may try a chain, for example a cheap classifier first and a stronger one on low confidence.
- A backend with `calibrated: false` cannot trigger ACT until a calibration profile exists for it.

Not in scope until there is a use: image classification, and a third-party backend plugin system. A registry dict plus a Protocol is enough.

## Classifier facts (measured on small synthetic samples; re-verify on real files)
- Open-Jev is a text-only 2B classifier. Choice questions return a label and probabilities.
- Confidence >= 0.8 was reliable; 0.4-0.8 was about a coin flip. Initial profile: act >= 0.8, review 0.4-0.8.
- Accuracy falls smoothly with label count. Keep 3-8 labels per node and always include `other`. This is guidance from measurement, not a hard limit.
- Label order does not matter. Descriptions did not measurably help.
- Multiple questions per request work. Batch axes (type, language, status) per file.
- Input over 4096 tokens returns HTTP 422 and is never truncated server-side. The client truncates to about 2000 characters and skips empty text.
- The server serializes requests (about 1.7 req/s regardless of concurrency). Use a single worker and no parallelism.
- Filename-only evidence never triggers an action.

## Descriptive tags (ADR-0005)
A user vocabulary of tags (`tree.json` defaults plus `config.json`, edited with `sift vocab add TAG DESCRIPTION`) is scored yes/no over up to 3 text chunks per file. Best-chunk score at or above the tag's act threshold is written, between review and act is suggested. Skipped for texts under 200 characters and for confident `data`/`log` files. `sift propose PATH` asks a local Ollama model for new tag names (never a `:cloud` model); proposals are only printed.

## Discovery and indexing (ADR-0006)
`sift discover` collects tag proposals, merges near-duplicates, tests each candidate with the classifier (own-file recall, other-file prevalence) and lists the useful ones; accepting adds the tag to the vocabulary. `sift index` is a budgeted, resumable background pass over `index_dirs`, newest first, run by a systemd user timer. Network and cloud drives, hidden and vendored directories are never walked, and source files inside git repositories are never tagged.

## Tree
`tree.json` holds nodes with instructions, question kind, and labels. Leaves are tag sets. Depth is 3-4 at most. The path doubles as a hierarchical tag (`work/project/spec`). The runner stops at the first node under its threshold and records a REVIEW outcome with the partial path.

## Security requirements (day one)
- Labels select only from a closed set of side-effect-free actions. File or OCR text is never interpolated into shell commands or notification markup.
- Tag names are sanitized to `[a-z0-9_-]` segments. Every source and destination is checked with `realpath` against allow-listed roots. Symlinks pointing out of the roots are never followed. Nothing is overwritten (`RENAME_NOREPLACE` when moves arrive).
- Before writing tags, `TagStore` re-verifies `(inode, mtime, size)` taken at classification time.
- The endpoint is user config and is never committed. Plain HTTP is accepted only for loopback (`127.0.0.1`, `::1`, `localhost`) or networks the user lists in `trusted_networks`. Any other plain-HTTP host is refused unless `allow_insecure` is set. HTTPS is always allowed.
- No generative tag proposals go to work-account services. Content never leaves for files flagged sensitive.
- Index and logs are mode 0600. Sensitive-class tags are never written as xattrs.
- Extractors (`pdftotext`, OCR) run sandboxed with size caps.
- The injection corpus is part of `sift eval`.
- Hostnames and IPs of personal servers must not appear in tracked files. A pre-commit check enforces this.

## Guardrails
| Risk | Check |
|---|---|
| Core gains I/O | `import-linter` contract: `sift.core` may not import `sift.adapters`, `os`, or `subprocess` |
| Behavior regressions | `pytest` with the fake classifier and recorded backend responses |
| Unsafe tags or paths | tests feeding hostile names (`../../.ssh`, symlinks out of roots) to `TagStore` |
| Hostname leaks | pre-commit grep over tracked files |
| Types | `ruff`, `mypy --strict` on `core` |

## Build order
1. `core/model.py`, `ports.py` (with capabilities), `core/runner.py`, and the fake classifier.
2. `adapters/jev_http.py` with endpoint trust, truncation, and 422 handling.
3. `sift eval`: sampler, labeled CSV (untracked), per-node accuracy, calibration table, injection corpus.
4. `sift scan <dir>` with the report store only.
5. `adapters/xattr_store.py`, only after the eval passes.

## Later (only if the eval justifies it)
- Watcher as a systemd user unit. A bar-widget plugin cannot host it. It acts only on `IN_CLOSE_WRITE` and re-verifies inode, mtime, size, and hash before any move.
- Review panel (Quickshell), undo log, bar widget.
- Screen triage via `omarchy capture text`.
- Duplicate and secret audits, tag suggestions, image backends.

## Viewing tags
Baloo and Dolphin read `user.xdg.tags`. Nautilus (the default file manager) needs a small `nautilus-python` column. Unverified: xattr survival on Syncthing and Drive.

## Running Open-Jev
Local: run the Open-Jev server on this machine and use `http://127.0.0.1:8791` (default).
Remote: put the URL in `config.json` (gitignored) or `SIFT_ENDPOINT`. Config precedence is env, then `config.json`, then `config.json.example` defaults.
