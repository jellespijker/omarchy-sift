# Architecture

How Sift is put together after the publication hardening (ADR-0013). Commands live in `sift/commands/` (`scanning`, `review`, `research` and the registry modules); `cli.py` is the parser and dispatch. Decisions are in [adr/](adr/); this page is the picture.

## Containers

```mermaid
flowchart LR
  user([User]) --> panel["Quickshell panel<br/>Panel / Model / Content.qml"]
  user --> cli["sift CLI<br/>cli.py + commands/"]
  timer["systemd timers<br/>index, discover"] --> cli
  panel -->|"argv, JSON out"| cli
  cli --> api["api.py<br/>Sift: classify, apply, escalate, profiles"]
  api --> core["core/ (pure)<br/>runner, tree, profiles, prompts, tune"]
  api --> adapters["adapters/<br/>jev, chat, ollama, tuner, extract, stores, http"]
  cli --> state[("state module<br/>tolerant read, locked append,<br/>atomic write, schema, compaction")]
  adapters --> state
  cli --> config[("config module<br/>layers, migrate, validate,<br/>locked atomic save")]
  adapters --> backends{{"classifier backends<br/>Open-Jev, Ollama, OpenAI-compatible"}}
  adapters --> fs[("files + xattr<br/>user.xdg.tags")]
```

## The state module

Every rule about how state is stored lives in `state.py`. Nothing else parses JSONL or writes a file its own way.

```mermaid
flowchart TB
  subgraph callers["Callers (no JSONL parsing of their own)"]
    c1[index / scan / watch]
    c2[review / accept / audit]
    c3[tags / tune / usage]
    c4[panel via CLI]
  end
  subgraph state["state.py: one owner of how state is stored"]
    r["read: skip bad bytes, torn lines,<br/>records missing required keys"]
    a["append: one write, under file lock"]
    w["write_atomic: unique temp, fsync, rename"]
    l["lock: advisory flock, dies with process"]
    s["schema: REPORT_SCHEMA, usable, is_current"]
    m["maintain: compact latest per key,<br/>cap undo, roll up usage"]
  end
  callers --> state
  state --> files[("report, reviews, verdicts,<br/>undo, merges, usage .jsonl<br/>index_status, notified .json")]
  m -.->|"after each index pass, only when file is big"| files
```

## An index run

```mermaid
sequenceDiagram
  participant T as systemd timer
  participant I as sift index
  participant S as state module
  participant K as Sift (classify)
  participant X as XattrStore
  participant F as file xattr
  T->>I: start
  I->>S: lock index.lock (non-blocking)
  I->>S: write_json index_running (pid, start ticks)
  loop batches of 25 files until budget or deadline
    I->>K: classify each file
    K-->>I: decision
    I->>X: apply tags (guard: xattr.flock)
    X->>F: read, merge, write xattr
    X-->>I: ok or StoreError
    I->>S: append report entry (failed write goes to review with reason)
    I->>I: reload config and tree (picks up renames, merges)
  end
  I->>S: write_json index_status
  I->>S: maintain (compact, cap, roll up)
  I->>T: exit 0, 3 aborted, 4 already running
```
