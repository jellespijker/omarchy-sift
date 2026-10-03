# ADR-0004: Quickshell plugin is UI only; the watcher is a separate user service

Status: Accepted

## Context
The Omarchy shell loads third-party plugins as QML (`manifest.json`, `Panel.qml`). A bar widget runs inside the long-lived shell process, which reloads whenever a file under the plugin directory changes. Existing plugins shell out to a script in `bin/` and keep their logic outside QML.

Sift needs a background component (watching folders, waiting for downloads to finish) and a review UI.

## Decision
- The plugin (`manifest.json`, `Panel.qml`) is UI only. It runs `bin/sift status|review|accept|reject|watch --once` as short-lived processes and parses their JSON output. It has no classification logic and no file access of its own.
- The watcher is `sift watch`, run as a systemd user unit (`contrib/sift-watch.service`, not installed automatically). It acts only on files whose size and mtime are unchanged between two polls and skips `.part`, `.crdownload` and dotfiles.
- All state lives in `$XDG_STATE_HOME/sift` (mode 0700), outside the plugin directory, so writing state never triggers a shell plugin reload. `bin/sift` sets `PYTHONDONTWRITEBYTECODE` for the same reason.
- Manual actions in the panel (accept, reject) are always allowed. Automatic tagging stays behind `write_xattrs`, which defaults to off. Accept refuses files flagged as possible secrets.
- Every xattr write is recorded in `undo.jsonl` with the previous value. `sift untag` restores it only if the file identity and the current tags still match what Sift wrote.

## Consequences
- The CLI is the integration surface: the panel, the watcher, scripts and agents all use the same commands.
- A broken classifier or a crashed watcher cannot take down the shell. The panel shows an error state instead.
- Polling (about 30 s) is simpler than inotify and fine for a downloads folder. inotify can replace it later without changing the CLI.
- Cost: each panel refresh spawns two short Python processes.

## Alternatives rejected
- **Logic in QML or a QML-hosted service:** untestable and tied to shell reloads.
- **State inside the plugin directory:** every write reloads the shell plugin.
- **Auto-enable the watcher on install:** the first run should be a dry run the user has looked at.
