# Changelog

## 0.3.0

Hardening for use on other people's machines (ADR-0013), plus prompts, profiles, escalation and tuning (ADR-0011, ADR-0012).

- **State module.** One owner for every state file: tolerant reads (damaged lines and records are skipped), locked single-write appends, atomic
  replacement, a schema constant, and compaction so the files cannot grow forever (report, undo, verdicts, usage rolled up by day).
- **Concurrency.** Config saves are locked and atomic. Tag read-modify-write is serialised. A long index run reloads settings between batches,
  so a tag renamed or merged in the panel is respected. A recycled process id is no longer mistaken for a running scan.
- **Config.** Legacy keys are migrated per file. Hand-edited values give a clear message, not a traceback. An empty folder list means no folders.
  `sift setup` keeps your other backends.
- **Safer by default.** Redirects to another host never carry credentials. Messages never print URLs with keys. Regexes that can backtrack
  catastrophically are refused (checked on the parse tree). More secret files and patterns are recognised. Extractors do not follow symlinks.
- **Failures are visible.** A failed tag write goes to review with the reason (it is no longer recorded as done). Missing `pdftotext`,
  `systemctl` or `notify-send` are reported. The panel shows when a part could not be loaded. Undo puts back only the file you changed.
- **Systemd.** Units quote the path, outlast the longest run, and ignore demo mode. `sift doctor` finds timers pointing at a moved plugin.
- **Defaults for everyone.** The packaged vocabulary no longer assumes one person's files (the `dutch` language tag is opt-in with `--detector`).
- **Structure.** `cli.py` is now parser and dispatch; commands live in `sift.commands.*`. The pure core is checked transitively.
- **Tests.** Fault-injection tests, a simulated no-xattr filesystem (`SIFT_TEST_NO_XATTR=1`), and CI.

## 0.2.0

First feature-complete version: pluggable backends, tag manager, review and audit, discovery, translations, demo mode.
