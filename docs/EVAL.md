# First evaluation (2026-10-02)

Backend: Open-Jev (Qwen3.5-2B decision head). Tree: `tree.json` (root: document/code/log/other; document: invoice/manual/meeting/correspondence/other).
Reproduce: `sift eval --builtin --sweep 0.5,0.6,0.7,0.8,0.9`.

## Synthetic corpus (112 files, seed 7)
The corpus is templated, so it flatters the model. Treat these numbers as an upper bound.

| Metric | Result |
|---|---|
| Root node accuracy | 95/112 (85%) |
| Document node accuracy (given correct root) | 11/11 |
| Calibration, step probability 0.6-0.8 | 49/49 correct |
| Calibration, 0.8-0.9 / 0.9-1.0 | 26/26 / 12/12 |
| Calibration, 0.4-0.6 | 14/25 (56%) |
| Calibration, below 0.4 | 5/11 (45%) |

Threshold sweep (files acted, precision, coverage):

| act threshold | acted | precision | coverage |
|---|---|---|---|
| 0.5 | 87 | 99% | 78% |
| 0.6 | 76 | 100% | 68% |
| 0.7 | 53 | 100% | 47% |
| 0.8 | 27 | 100% | 24% |
| 0.9 | 1 | 100% | 1% |

Prompt injection (16 clean/injected pairs): 4 label flips at the default profile. No injected file was steered to the attacker's requested label with high confidence. The visible effect was lower confidence, which moves a file from ACT to REVIEW. One pair moved the other way, which looks like noise.

End-to-end write test (xattr store, act threshold 0.6, 48 files): 33 tagged, 15 skipped, 0 wrongly tagged.

## Real files (100 random files from a personal downloads folder, no ground truth)
- 0 files reached ACT at the 0.8 threshold. 72 went to review and 18 were undecided.
- 21 of 90 classified files had no extractable text (PNG, DOCX, ZIP, PPTX, others) and were classified by filename only, so they can never act.
- 58 files were labeled `log`, at 0.37-0.70 confidence. The ones inspected were tiny download-error stubs, so that label is plausible.
- One Google OAuth `client_secret_*.json` file was labeled `code` at 0.89 and would be tagged. Nothing in the current tree flags secrets. This supports the planned sensitive-file audit.
- 10 requests failed with connection errors because the server went away for a short time. The scanner now retries twice and aborts after 5 consecutive failures. An earlier full-folder scan (2,682 files) stalled after 5 files for a reason I did not find.

## Conclusions
1. The classifier is reliable above about 0.6 on clean, templated text. The default 0.8 act threshold gives very low coverage. A per-backend profile of act 0.6-0.7 looks better, but it is only justified by synthetic data.
2. The synthetic result does not transfer. Real downloads are mostly text-less or unusual, so coverage will be low whatever the threshold.
3. Real-file accuracy is unknown. It needs hand labels.

## Next steps
- Fill in `label` in `eval/labels.csv` (100 sampled real files, untracked) and run `sift eval eval/labels.csv --sweep 0.5,0.6,0.7,0.8`. Choose the act threshold from that.
- Add labels the real folder needs. Candidates: `data` (measurements and tables), `image`, `archive`, `secret`.
- Add OCR for images and a docx/xlsx text extractor, since many real files are text-less.
- Do not enable `write_xattrs` until the real-file numbers exist.
