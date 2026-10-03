# Publishing checklist and announcement drafts

## Before the first public commit

- [ ] Replace `<git-url>` in the README (twice) with the repository address.
- [ ] Decide the plugin id is final: `jellespijker.sift` (manifest, `Panel.qml`, IPC target, systemd path in `contrib/`). Changing it later breaks installs.
- [ ] Tag the release (`v0.3.0`) after the first commit; `manifest.json`, `pyproject.toml` and `CHANGELOG.md` already say 0.3.0.
- [ ] Confirm nothing personal is tracked: `git ls-files | xargs grep -Il "/home/\|@"` (the test suite also checks for private hosts). `eval/*.csv`,
      `config.json` and `.impeccable/` are ignored.
- [ ] Run `python3 -m pytest -q` and `SIFT_TEST_NO_XATTR=1 python3 -m pytest -q`; push and check the GitHub Actions run (never run yet).
- [ ] Try a clean install as another user or in a fresh `HOME`: `omarchy plugin add`, `sift setup`, `sift doctor`, `sift demo on`.
- [ ] Re-render screenshots if the UI changed: `python3 tools/render.py --demo-home /tmp/sift-demo --themes tokyo-night --tabs review,audit,tags,settings --out docs/screenshots`.
- [ ] Ask a Dutch, German, French, Spanish, Chinese and Arabic speaker to read the panel (translations are model-drafted).

## Known limits to state up front

- Tested against Open-Jev and Ollama only; other OpenAI-compatible servers and the hosted TypeSafe auth header are unverified.
- Real tag precision depends on your files and model. Sift ships the tools to measure it (`sift audit`, `sift eval-tags`) but no benchmark numbers.
- Tags live in extended attributes: filesystems without them cannot hold tags, and copy or sync tools must preserve them.
- Source files inside git repositories are not tagged on purpose.
- Needs Omarchy's Quickshell shell for the tray; the CLI works anywhere with Python 3.12+.

## Announcement (long form)

**Sift: let your own model tag your files, and keep the tags as a standard**

I got tired of files named `scan-0042.txt`. Sift is an Omarchy plugin that reads what is inside your files, asks the classifier *you* choose what
they are about, and writes the answer as standard `user.xdg.tags`. Dolphin and Baloo pick them up immediately; so does `getfattr`.

The part I care about is that you decide where the thinking happens.

- Run it fully local: Open-Jev, or any Ollama / llama.cpp / vLLM model through an OpenAI-compatible endpoint. Nothing leaves your machine.
- Or use a commercial API through the same interface, with a token and cost counter in the tray.
- Or both: a small local model does the first pass and a stronger one only re-judges what the first could not decide.

It is careful rather than clever. High-confidence results are written, middling ones wait in a review queue, and files that look like secrets are
never read by any model. Every accept, dismiss and own tag you give becomes a verdict that `sift audit` turns into measured per-tag precision, and
`sift tune` can propose better prompts and keyword gates, keeping only the ones that beat the current ones on your files.

Edit the prompts, define your own tags in one sentence, use different rules per folder or file type, merge tags you do not like (and unmerge
them). The panel follows your Omarchy theme and speaks seven languages, right-to-left included. There is a demo mode so you can look around
before it touches a single real file.

Everything is plain Python 3.12 with no dependencies, MIT licensed, and the tags stay on your files if you ever uninstall it.

## Short form (forum or chat)

> Sift, an Omarchy tray plugin: your own LLM (local Ollama/Open-Jev/llama.cpp or a commercial API) tags your files with standard
> `user.xdg.tags`, so Dolphin/Baloo and any script can use them. Review queue, measured precision (`sift audit`), prompt and per-folder
> profiles, no database or lock-in. Demo mode included.

## One line (plugin list)

Tag your files with the AI of your choice, local or commercial, as standard `user.xdg.tags` that Dolphin and other tools read.

## Topics / keywords

omarchy, quickshell, hyprland, file-tagging, xattr, xdg-tags, dolphin, baloo, ollama, llm, local-first, privacy
