# ADR-0011: Editable prompts and per-folder, per-type profiles

Status: Accepted

## Context
Few tags were found because one global vocabulary and one global question wording were applied to every file. A PDF was asked "is this
code?", a folder of invoices was scored for `3d-printing`, and nothing could be tuned without editing code. Users need to change the wording
the models see, and to say "in this folder, for these files, do this instead".

## Decision
**Prompts are data.** Three templates are editable: `tag_question` (`{tag}`, `{description}`), `chat_system` (no placeholders) and `propose`
(`{n}`, `{existing}`), plus the question text of any tree node (`nodes.<id>`). Defaults live in `core/prompts.py`. `render()` replaces only the named
placeholders, so a template cannot run code or reach other data. `valid_prompt` removes control and bidi characters, limits length to 1500 and
rejects unknown placeholders.

**What cannot be edited.** The sentence that file text is data and not instructions, and the JSON output format the parsers read, are added by
the adapter after the user's text. A prompt can therefore change what is asked, not weaken the injection defence or break parsing.

**Profiles.** `profiles` is a list of `{name, match: {dirs, extensions, globs}, skip, tags, vocabulary, prompts, exclude_labels, act, review}`.
All given criteria must match. The most specific profile wins: longest directory, then most criteria, then list order. A profile derives a changed
copy of the tree (`core/profiles.derive`; the base tree is never mutated); `Sift` caches one derived tree per profile. `skip` leaves files
alone, also in `sift index`. `tags: []` scores no descriptive tags, a list allows only those, `vocabulary` adds tags that exist only there.
`exclude_labels` removes labels from the first question. A packaged `pdf` profile removes `code` (154 of 244 PDFs had been routed to it). A user
profile with the same name replaces a packaged one.

**Tags per kind of file.** A vocabulary entry may carry `labels` (first-question labels it applies to). General tags apply unless the label is in
`vocabulary_skip`; a profile can change that list (`skip_vocabulary`). Packaged code and log tags use `labels`, so logs get `crash-log` but not `invoice`.

**Gates suggest, they do not erase.** When the model says yes but the keyword gate fails, the tag is offered in review with the reason
"suggested without keyword evidence" and is never written automatically. Precision is kept and the lost recall becomes visible.

**Failure.** A bad prompt or profile is skipped and listed by `sift doctor`; it never stops a scan.

**Interface.** `sift prompts list|set|reset`, `sift profiles list|add|remove|test`. `~` in profile directories is expanded outside the pure core.

## Consequences
- Changing a prompt does not invalidate cached results; files are re-scored when they change. A forced re-score is future work.
- Profiles multiply behaviour, so `sift profiles test PATH` shows which one applies.
- Per-profile backend choice, per-profile `chat_system` and `propose`, and a panel editor are not built.
- Guardrails: tests for validation, specificity, derivation, skipping, packaged defaults and CLI round-trips (`tests/test_profiles.py`); the core purity test covers `core/profiles.py`.
