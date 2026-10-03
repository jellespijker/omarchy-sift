# ADR-0006: Tag discovery is propose, cluster, test, then accept; indexing is a budgeted timer job

Status: Accepted

## Context
Descriptive tags come from a vocabulary (ADR-0005) that was edited by hand. A first survey of 400 files with a small local model produced 1,552 distinct tag names. Many were near-duplicates (`build-script`, `script-build`) and many described a file's form rather than its subject (`readme`, `html-file`). The home folder holds about 20,000 files worth classifying, the classifier handles roughly 1-2 files per second and serializes requests, and the machine is often heavily loaded.

## Decision
**Discovery** (`sift discover`) is a pipeline in which a human admits every tag:
1. `collect` asks a proposer model (Ollama, never a `:cloud` model) for tag names on prose files that have none yet.
2. `run` merges near-duplicates (order- and plural-insensitive), drops tags already in the vocabulary or previously rejected, and requires a minimum number of supporting files.
3. Each surviving candidate is tested as a yes/no tag with the classifier on its own files plus other files. It is useful when its own files score at or above 0.6 at least half the time (recall) and at most a quarter of the other files do (prevalence). Generic tags fail on prevalence.
4. `list` shows the useful candidates. `accept` adds the tag to the user vocabulary in `config.json`. `reject` records the decision so it does not reappear. The panel shows candidates as chips: click to accept, × to dismiss.

**Indexing** (`sift index`) classifies eligible files under `index_dirs` within a budget (default 400 files or 25 minutes per run), newest first, and resumes where it stopped. It is run by a systemd user timer every 30 minutes after the previous run ends. A weekly timer runs discovery.
- The shared walker (`sift.walk`) never follows symlinks and never enters mount points, cloud-sync folders such as `GDrive*`, `OneDrive*` or `Dropbox*`, hidden folders, dependency or build directories, or `google-cloud-sdk`.
- Inside git repositories only prose files (Markdown, text, PDF, office documents) are classified. Source code is never tagged.
- A lock file prevents overlapping runs. Units use `Nice=15` and idle I/O priority.
- Proposer models are chosen in `config.json`; the default is a small Gemma model on the same Ollama server as the classifier.

## Consequences
- The vocabulary grows from evidence in the user's own files, and generic or noisy tags are filtered by a measurement before the user sees them.
- A full pass over about 20,000 files takes days at the default budget. Newest files are handled first, so recent work is tagged early.
- Candidate tests are only as good as the classifier. A tag the classifier cannot recognise (for example a CMake-specific tag on `CMakeLists.txt`) is dropped even if it is useful.
- Cost: extra moving parts (two timers, a lock, state files) and a recurring load on the classifier server.

## Alternatives rejected
- **Auto-adding discovered tags:** unbounded vocabulary drift and a larger injection surface.
- **Classifying the whole home folder in one run:** days of continuous load, and no partial result until the end.
- **Including source files in repositories:** thousands of tags on code with little value, and writing attributes into working trees.
