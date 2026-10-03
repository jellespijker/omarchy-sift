# ADR-0008: Validated input, tags in any script, existing tags, and tag evaluation

Status: Accepted

## Context
Review of the tag manager found real defects: tag names in Chinese or Arabic were reduced to an empty string and accented Latin was mangled;
an invalid evidence pattern crashed a scan and a pattern such as `(a+)+$` took 19.5 seconds on 29 characters, enough to freeze the
indexer; a file tagged by another tool with non-UTF-8 bytes crashed the writer; the tag manager only knew tags Sift itself had recorded; tag
changes skipped any file edited after classification; and the only evaluation covered the decision tree, not the user's tags or backends.

## Decision
- `sift.validate` is the one place that cleans and checks user input. Tag names keep letters, digits and combining marks in any script and
  turn everything else into `-` (so commas, slashes, control and bidirectional-override characters cannot survive); descriptions are
  normalised, stripped of control characters and limited to 300 characters; patterns must compile, be short and avoid nested quantifiers;
  thresholds and counts are range-checked. Every command and the tree loader use it. A bad entry in a hand-edited config is skipped and
  reported, never fatal, and evidence gates fail closed.
- Tags are read and written through one pair of helpers that treat the attribute as bytes (`surrogateescape`), so foreign encodings survive.
- `sift tags scan` reads the tags actually on files (read-only, cached) and the manager shows them as `yours`. Tag changes use each file's
  current state, not the stale classification reference, and keep every other tag.
- `sift tags edit` changes description, settings and name in one step; the panel editor saves on Enter.
- `sift eval-tags` measures per-tag precision and recall over a threshold sweep for any backend, from a sheet pre-filled with Sift's own
  answers, and can store thresholds that meet a precision target.

## Consequences
- Tags and descriptions work in any script; the interface text is still English only. Translating it (Qt `qsTr` and translation files, and
  right-to-left layout mirroring) is future work.
- The nested-quantifier check is a heuristic, not a proof: Python's `re` cannot be interrupted, so unusual pathological patterns could
  still be slow. Patterns are also limited to 200 characters and run on at most 32 KB of text.
- The classifier determines how well non-English descriptions work. That is measured with `eval-tags`, not assumed.

## Alternatives rejected
- **ASCII-only tags:** simple, but excludes most of the world's languages.
- **A regex engine with timeouts:** a new dependency for a feature most users will not touch.
- **Treating foreign tags as read-only forever:** users expect one manager for all their tags.
