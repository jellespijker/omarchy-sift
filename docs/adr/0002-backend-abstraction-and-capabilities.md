# ADR-0002: Backend abstraction, evidence parts, and capabilities

Status: Accepted

## Context
The first backend is Open-Jev, a text-only 2B classifier that answers choice questions with per-label probabilities. A first sketch of the `Classifier` port took text and returned choice answers. That fits Open-Jev but leaks in three places:
1. Text-only evidence cannot carry an image.
2. Only one question kind (`choice`) exists, while multi-label tagging needs yes/no per tag.
3. A global confidence threshold assumes every backend's probabilities mean the same thing. The 0.8 threshold came from measurements on Open-Jev only.

Changing `Evidence`, the question types, or the decision record later would break the port, every test, and any stored decisions.

## Decision
- `Evidence` is a bundle of parts: `{text?, image?, filename, mime, metadata}`. v1 implements text and filename.
- Questions are a tagged union. `Choice(labels)` and `Multi(tags)` are implemented. `Score` and `Extract` are reserved names only.
- Each backend declares capabilities: `{modalities, kinds, max_labels, max_chars, calibrated}`. The runner reads them for truncation, label-count checks, and routing.
- Thresholds live in a per-backend profile produced by `sift eval`, not in the tree. A node requests a confidence level and the profile maps it to a number. A backend with `calibrated: false` cannot trigger ACT until a profile exists.
- Backends are registered by name in `config.json`. A node or modality chooses one, and a router may try a chain (cheap first, stronger on low confidence).

## Consequences
- A second service or an open-source project (a local LLM with logprobs, a zero-shot NLI model, an embedding classifier, a rules baseline) is one new adapter.
- Adding categories or axes is a `tree.json` edit. Accuracy limits are found by the eval, not hardcoded.
- Image support needs a new backend and an extractor, not a port change.
- Cost: about 40 extra lines and one concept (capabilities). Mismatched profiles are possible if the eval is skipped, which is why uncalibrated backends cannot act.

## Alternatives rejected
- **Port shaped exactly like the Open-Jev API:** cheapest today, but every later change touches the core and stored decisions.
- **A third-party backend plugin system:** unneeded for one maintainer. A registry dict and a Protocol are enough.
- **Implementing image classification now:** Open-Jev cannot see images, and there is no use case yet. The port only has to accept it later.
