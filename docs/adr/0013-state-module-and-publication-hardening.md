# ADR-0013: One owner for state, and hardening for use by other people

Status: Accepted

## Context
Sift was written for one user on one machine. A review before publishing (read of the whole code base, with four findings re-checked by running
them) found that its weak points were not features but the edges: every module parsed JSONL its own way, so one damaged byte made every command
and the panel fail; writes were not atomic and the config save lost concurrent updates; a long index run wrote back tags that had since been renamed
or merged; the report format version was hard-coded in about eight places, so a format change would have re-classified every user's files (and
spent their tokens); a failed tag write was recorded as done; and missing tools, a filesystem without xattrs, or a hand-edited config produced
stack traces. ADR-0001 required a pure core, but the purity test only inspected direct imports, so the core still reached the language catalogue
through `validate`.

## Decision
**A deep `state` module.** `state.py` owns every rule about how state is stored; no other module parses JSONL or writes a state file itself.
- Reads are tolerant: invalid bytes, torn lines, non-objects and records missing the keys their file always has are skipped.
- Appends are one `write` on an `O_APPEND` descriptor under a per-file advisory lock; whole-file writes use a unique temp file, `fsync` and rename.
- `REPORT_SCHEMA` and `REPORT_READABLE` are the only place the report version appears (`usable`, `is_current`). A release that adds a format
  lists both, so older entries stay valid and nobody is re-scanned.
- `maintain()` runs after each index pass and only on big files: newest record per file (report, reviews), per file and tag (verdicts, merges),
  an undo cap, and usage older than 30 days rolled up by day with exact totals.

**Config is validated and migrated on load.** Legacy keys are renamed per file (so a packaged default cannot shadow an older user setting). Values a
hand edit can break give a message naming the setting. Saves hold a lock and replace the file atomically.

**Failures are first-class.**
- Tags are written before the report entry. A failed write goes to review with the reason; five in a row abort the run; the index probes
  xattr support first and degrades to reporting only.
- The panel shows which part could not be loaded and why. The CLI turns unexpected errors into one line (`SIFT_DEBUG=1` shows the traceback).
- Redirects to another host are refused, so credentials cannot follow them. Messages print endpoints without credentials or query.
- Regex gates are checked on the parse tree (an unbounded repeat containing an alternation or another unbounded repeat is refused).
- Undo is by file (`sift untag --path`), and reverting everything needs `--all`.

**Amends ADR-0001.** The pure core is enforced transitively: a test follows every `sift.*` import from `core/` and fails if it reaches the file
system, processes, network, state, config or i18n. Validation moved to `core/validation.py` with an injected translator.

**Structure.** `cli.py` is parser and dispatch; commands live in `sift.commands.scanning`, `.review` and `.research` next to the registry
modules. One function adds a vocabulary tag.

## Alternatives rejected
- **SQLite for state.** Real fault tolerance and queries, but it replaces files users can read, back up and delete, adds schema migration, and
  every consumer changes at once. Revisit if the report outgrows compaction (more than about a million files).
- **A persistent index for status polling.** Compaction already bounds the report to one line per file; an index adds a second source of truth.
- **Running regex gates in a subprocess with a timeout.** Costly per file; the parse-tree check removes the dangerous shapes instead.

## Consequences
- Old per-file history in `report.jsonl` is gone after compaction (only `sift tags unmerge` for merges made before its journal used it).
- Every state write now takes a lock; measured cost is negligible next to a classifier call.
- Tests: `tests/test_robustness.py` (damaged state, concurrency, upgrade, config, redirects, missing tools, symlinks, failed writes, regex and
  secret cases), the transitive purity test, and a simulated no-xattr filesystem. CI runs all three ways.
- Not done: per-profile backends, SQLite, a `sift maintain` command (maintenance runs after index), translating the remaining English-only CLI text.
