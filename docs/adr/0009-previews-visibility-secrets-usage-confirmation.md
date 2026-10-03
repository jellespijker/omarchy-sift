# ADR-0009: Floating previews, visible classifier, keyring secrets, token usage, confirmed destructive changes

Status: Accepted

## Context
Reviewing tags needs the real file in front of you; the panel did not say which classifier was in use; hosted services need a key that is
stored safely and also works for scheduled runs; users of paid services want to know what a scan will consume; and tag changes that rewrite
many files were one click away. A `.md` preview opened a web app because that is the desktop default.

## Decision
- `sift open` launches the desktop default (or a per-type override, never via a shell) and, under Hyprland, floats, sizes and centres the new
  window by address. Dispatch uses the Lua API of current Hyprland (`hl.dsp.window.float/resize/center`, verified on 0.56.2) and falls back to
  the classic dispatchers. The command returns when the window has been handled, so the panel spinner is exactly as long as the wait.
- `describe_backends` is the single plain-language description of what is configured (model, role, automatic or suggest-only, local or remote,
  key source). The panel Settings, the header and `sift backends` all use it.
- API keys may come from the keyring (`secret-tool`, key on stdin only), a private file, an environment variable or a command; scheduled runs
  work with the first two and the command. `sift secret` manages keyring entries; the setup wizard offers the keyring first.
- A usage ledger records, per request, the tokens reported by the service, or an estimate flagged as such, plus a marker per classified file;
  it never stores file text. Summaries cover today, 7 days and total, optional per-backend prices give a cost, and the scan estimate is
  tokens per file times files remaining once at least five files have been measured.
- Delete, merge and rename report the number of files affected (via `--dry-run`) and require confirmation: a card in the panel, a prompt on a
  terminal, `--yes` for scripts. Marking a tag wrong during an audit is frequent, so it gets an Undo instead of a question.

## Consequences
- Preview behaves the same for every file type, and the surprising defaults can be changed without touching the desktop settings.
- Hyprland's dispatch syntax changed between releases; the fallback keeps older versions working but only the new syntax is tested live.
- Usage numbers are exact only when the service reports them. The remaining-scan estimate is rough and says so.
- Keyring storage needs a running, unlocked secret service; `sift doctor` reports a missing or locked one.

## Alternatives rejected
- **Rendering previews inside the panel:** large effort per file type and not "the default viewer" the user asked for.
- **A confirmation before every audit verdict:** it would make the audit unusable; undo covers the risk.
- **Estimating tokens only:** the services that bill by token already report them.
