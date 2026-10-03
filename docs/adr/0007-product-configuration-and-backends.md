# ADR-0007: A configurable product: user config, pluggable backends, tag manager

Status: Accepted

## Context
Sift began as one person's tool wired to one Open-Jev server. To distribute it as an Omarchy plugin it has to run for anyone: with a
local or hosted Jev service, with an ordinary chat model, with different folders, scan frequencies and tag sets, and without
personal hostnames or secrets in the repository. The tray must not turn into a log of everything the indexer does.

## Decision
**Configuration.** Layered and per user: packaged defaults, a legacy `config.json` next to the plugin (read-only compatibility), the
user file `~/.config/sift/config.json` (mode 0600, written atomically), and environment overrides. All writes go to the user file.
Tests run against private config and state directories.

**Backends** are a named table (`backends`), each with a `type`:
- `jev`: the decision API (`POST /v1/systemone`). Covers Open-Jev, the hosted TypeSafe API and gateways that pass the same body through.
  `model`, `path`, `auth_header`, `auth_scheme` and `health_path` are configurable. Calibrated.
- `chat`: any OpenAI-compatible chat endpoint. The model returns a label and a stated confidence, spread into probabilities. Uncalibrated,
  so per ADR-0002 it can only suggest until the user gives it thresholds (`sift setup --trust` or `act`/`review` in the entry).

Secrets are referenced, never stored in the JSON: a private key file, an environment variable, or a command. Remote endpoints need
explicit per-backend consent; plain HTTP is limited to this machine and listed networks. The tag proposer (`ollama` or `chat`) is
configured the same way.

**Folders and schedule.** One `dirs` list and one `ignore` list (names, globs or paths). The schedule is generated systemd user timers
(`sift schedule set`), so units never contain hand-edited paths.

**Tags.** The vocabulary is user data. Renames, merges and deletions are `tag_map` entries (`""` means drop) plus rewriting of existing
files through the undo log; packaged defaults are hidden with `disabled_tags` rather than edited. A merge keeps scoring both
phrasings and emits one tag. Candidate merges come from word order, plural and spelling similarity.

**Quiet tray.** The review queue shows only items with a suggestion for a user vocabulary tag, or possible secrets; generic guesses
are counted and hidden. Notifications are limited to classifier outages (once per outage), the first completed scan and new tag ideas
(weekly at most), each opt-out, none per file. The panel has Review, Tags and Settings tabs and no business logic of its own.

## Consequences
- A new user goes from install to first tags with `sift setup`; `sift doctor` explains failures.
- Hosted services have a privacy cost that is stated and consented to rather than assumed.
- A chat model needs a short trust-building period before it tags on its own, which is slower but honest about its confidence.
- The CLI remains the one integration surface for the panel, timers and scripts.
- Cost: more configuration surface to document and test; the hosted TypeSafe auth header is taken from a gateway's documentation and is
  configurable in case the direct API differs.

## Alternatives rejected
- **Free-form endpoint URL only:** cannot express auth, models or the chat protocol, and invites sending files somewhere unintended.
- **Keys in the config file:** they leak through backups, screenshots and support requests.
- **A settings UI that edits files directly:** duplicated logic. The panel calls the same CLI.
- **Notifying per review item:** the failure mode the redesign exists to avoid.
