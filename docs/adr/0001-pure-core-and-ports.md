# ADR-0001: Pure core with three ports

Status: Accepted

## Context
Sift classifies files with a small remote model and tags them. The model is unreliable, serializes requests, and sees hostile file content. A single maintainer owns the project. The plan grows from a CLI to a daemon to a Quickshell panel, and every future front end needs the same decision record (path, per-node probabilities, outcome).

Two risks drive the structure:
- Classifier behavior and file safety could become tangled, so tests would need a live server.
- A label could reach a shell or a file operation through a side-effecting code path.

## Decision
- A **pure core** (`sift.core`) takes evidence and returns a `Decision`. It performs no I/O, runs no subprocesses, and touches no paths.
- Three **ports** (`typing.Protocol`) cover the places where I/O happens: `Classifier`, `Extractor`, `TagStore`. Each already has two implementations, which is the justification for a seam: a real and a fake classifier, a report-only and an xattr tag store, and extractors per file type.
- The public interface is two calls: `classify(path) -> Decision` and `apply(decision, store)`. `apply` writes tags only when the outcome is ACT. The CLI, a later watcher, and the Quickshell panel all use these.
- `TagStore` is the only code that writes. It owns tag sanitization, `realpath` allow-listing, and re-verification of `(inode, mtime, size)`.
- The decision tree is data (`tree.json`), interpreted by one runner, not a class per node.
- The Quickshell plugin shells out to `sift scan --json`. This is the process boundary.

## Consequences
- Tests run against a fake classifier and recorded responses, with no server needed.
- The "label becomes an action" bug cannot occur in a module with no side effects.
- Cost: three Protocols and an adapters package for a small tool, and discipline to keep `core` free of I/O.
- Exit: if the eval shows the classifier is not viable, delete the HTTP adapter and tree runner and keep the extractor and tag store. Collapsing the ports into one module is a mechanical refactor.

## Alternatives rejected
- **Full hexagonal/DDD with an event bus:** one maintainer, one bounded context. Three ports with two implementations each is the whole justification.
- **Single script with inline HTTP calls:** acceptable for a spike, but the eval needs a fake classifier and the safety rules need one chokepoint.
- **Daemon plus SQLite plus IPC now:** adds drift and lifecycle problems before the eval shows value. It can be added later behind the same two calls.
- **Tree as code (a Strategy class per node):** a node is data, and a JSON file is editable without a release.

## Guardrails
`import-linter` contract (`sift.core` may not import `sift.adapters`, `os`, or `subprocess`), `pytest` with the fake classifier, hostile-name tests for `TagStore`, `ruff` and `mypy --strict` on `core`.
