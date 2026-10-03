"""Demo mode: the real program running against a sandbox of invented data, so every screen can be shown and every action tried without
touching real files or settings. Active when `SIFT_DEMO=1` or when the marker file `<real config dir>/demo-mode` exists.

The sandbox has its own config, state and a folder of synthetic files carrying real tags; the classifier is an offline stand-in."""
from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path
from typing import Mapping


def _real_config_dir(env: Mapping[str, str]) -> Path:
    return Path(env.get("SIFT_CONFIG_DIR") or Path(env.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "sift")


def active(env: Mapping[str, str] | None = None) -> bool:
    env = os.environ if env is None else env
    flag = env.get("SIFT_DEMO", "").lower()
    if flag in ("0", "false", "no"):                  # scheduled jobs set this: demo mode in the panel must never redirect real scanning
        return False
    return flag in ("1", "true", "yes") or (_real_config_dir(env) / "demo-mode").exists()


def home(env: Mapping[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    return Path(env.get("SIFT_DEMO_HOME") or Path.home() / ".local/share/sift-demo")


def paths(env: Mapping[str, str] | None = None) -> dict[str, Path]:
    h = home(env)
    return {"config": h / "config", "state": h / "state", "files": h / "files"}


def set_marker(on: bool, env: Mapping[str, str] | None = None) -> None:
    env = os.environ if env is None else env
    marker = _real_config_dir(env) / "demo-mode"
    if on:
        marker.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        marker.write_text("1\n")
    else:
        marker.unlink(missing_ok=True)


# ---- the invented data ---------------------------------------------------------------------------------------------------

DAY = 86400

FILES: list[tuple[str, str, list[str]]] = [
    # path, text, tags written by Sift
    ("Taxes/tax-return-2025.md", "Tax return 2025. Income statements, deductions and the assessment letter from the tax office. Amount due EUR 412.", ["tax-return", "税务"]),
    ("Taxes/receipt-bakker-hardware.txt", "Receipt Bakker Hardware. Invoice 20931. Total due EUR 86.40 incl. VAT. Payment within 14 days.", ["invoice", "فاتورة"]),
    ("Taxes/receipt-printing-supplies.txt", "Invoice 4411 from Acme BV. PLA filament 1kg, nozzle 0.4. Total due EUR 90.00, VAT 21%.", ["invoice"]),
    ("Taxes/scan-0042.txt", "Scanned page. Illegible header. Amount 12.50.", []),
    ("Career/interview-transcript-orbit-labs.txt", "Uh so thanks for having me. Ehm, I have worked as a firmware engineer for six years and the role you describe, uh, fits my experience. What is the salary range and the team size? Um, you know, I mean the interview went well.", ["transcript", "job-hunt"]),
    ("Career/job-offer-notes.md", "Job offer from Orbit Labs. Role: senior firmware engineer. Salary 5.800 per month, start in January. Candidate questions about the team and the vacancy.", ["job-hunt"]),
    ("Career/cv-draft.md", "Curriculum vitae. Work experience, skills: C++, Python. Education. Candidate profile for the vacancy.", []),
    ("Printing/extrusion-notes.md", "Extrusion experiments on a 3D printer: nozzle temperature, filament drying and retraction. Hypothesis: wet filament causes stringing. Research findings in the table below.", ["3d-printing", "research"]),
    ("Printing/pla-datasheet.txt", "Material datasheet PLA filament for FDM printers. Recommended nozzle temperature 200-220 C, print speed 60 mm/s, bed 60 C.", ["3d-printing"]),
    ("Printing/benchy.stl", "solid benchy", []),
    ("Printing/bench-run-2026-09.csv", "t,temp,flow\n0,200,1.0\n1,201,1.02\n2,200,0.99\n", []),
    ("Projects/roadmap-q4.md", "Meeting notes roadmap Q4. Agenda, attendees, action items and decisions. Minutes of the planning meeting.", ["meeting-notes", "meeting-note"]),
    ("Projects/standup-2026-10-01.md", "Meeting minutes standup. Attendees: Anna, Pieter. Action items: update the plan by Friday. Agenda: release.", ["meeting-notes"]),
    ("Projects/build-system-notes.md", "Notes on the build system: CMake presets, the CI pipeline and docker compose for the test runner. Dependency management with conan.", ["build-system"]),
    ("Projects/readme.md", "Project readme. Install with pip. Usage examples and licence information.", []),
    ("Projects/design-review.md", "Design review of the sensor module. System design, interfaces and failure modes. Research on alternatives.", ["research"]),
    ("Home/contract-lease-agreement.md", "Lease agreement between the parties. Clause 4 liability, terms and conditions, obligations of the tenant and confidentiality.", ["contract"]),
    ("Home/insurance-policy.md", "Insurance policy terms. The agreement covers liability and damages. Clause 7 obligations.", ["contract"]),
    ("Home/holiday-plan.md", "Holiday plan for the summer: flights, hotel and a packing list.", []),
    ("Home/gesprek-notities.txt", "Dit is een tekst over de planning van het project en wat we nog moeten doen om het af te maken. Eh ja, we hebben het er met de hele groep over gehad.", ["dutch", "transcript"]),
    ("Photos/IMG_2041.png", "", ["image"]),
    ("Photos/IMG_2042.png", "", ["image"]),
    ("Archive/backup-2025.zip", "", ["archive"]),
    ("Misc/lorem.txt", "lorem ipsum dolor sit amet", []),
]

# Files Sift is unsure about: (path, suggested tags with scores, reason)
REVIEW: list[tuple[str, dict[str, float], str]] = [
    ("Taxes/scan-0042.txt", {"invoice": 0.58}, ""),
    ("Career/cv-draft.md", {"job-hunt": 0.47}, ""),
    ("Projects/readme.md", {"build-system": 0.44}, ""),
    ("Home/holiday-plan.md", {"meeting-notes": 0.41}, ""),
    ("Printing/bench-run-2026-09.csv", {"research": 0.52, "3d-printing": 0.49}, ""),
    ("Misc/lorem.txt", {}, "possible secret: not sent to any classifier, never tagged"),
]
GENERIC_HIDDEN = [("Misc/hidden-guess-%02d.txt" % i, ["log"]) for i in range(1, 8)]

VOCABULARY = {
    "tax-return": {"description": "tax returns, assessments and letters from the tax office"},
    "build-system": {"description": "build systems, CI pipelines and dependency management", "act": 0.7},
    "税务": {"description": "税务文件、报税表和发票"},
    "فاتورة": {"description": "فواتير وإيصالات ومدفوعات"},
}

IDEAS = [
    {"tag": "insurance", "support": 9, "aliases": ["insurances"], "examples": ["insurance-policy.md", "claim-letter.md"], "recall": 0.8, "prevalence": 0.06, "good": True, "description": "The text is about insurance."},
    {"tag": "firmware", "support": 7, "aliases": [], "examples": ["bootloader-notes.md"], "recall": 0.9, "prevalence": 0.1, "good": True, "description": "The text is about firmware."},
    {"tag": "holiday", "support": 5, "aliases": ["vacation"], "examples": ["holiday-plan.md"], "recall": 0.7, "prevalence": 0.04, "good": True, "description": "The text is about holiday."},
    {"tag": "sensor-calibration", "support": 4, "aliases": [], "examples": ["design-review.md"], "recall": 0.6, "prevalence": 0.12, "good": True, "description": "The text is about sensor calibration."},
]

USAGE_PRICES = {"input_per_million": 2.0, "output_per_million": 8.0}


def _png() -> bytes:
    import base64
    return base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


def build(env: Mapping[str, str] | None = None, now: float | None = None) -> Path:
    """(Re)create the sandbox. Returns the folder of synthetic files."""
    from . import config as cfgmod
    from .adapters.stores import write_tags
    p = paths(env)
    for d in (p["config"], p["state"], p["files"]):
        shutil.rmtree(d, ignore_errors=True)
        d.mkdir(parents=True, exist_ok=True, mode=0o700)
    now = now or time.time()
    report: list[dict] = []
    for i, (rel, text, tags) in enumerate(FILES):
        f = p["files"] / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        if rel.endswith(".png"):
            f.write_bytes(_png())
        else:
            f.write_text(text + "\n")
        age = (i % 9) * DAY
        os.utime(f, (now - age, now - age))
        if tags:
            try:
                write_tags(f, sorted(tags))
            except OSError:
                pass
        st = os.stat(f)
        report.append({"path": str(f), "ref": [st.st_ino, st.st_mtime_ns, st.st_size], "outcome": "act" if tags else "review", "tags": tags,
                       "suggested": [], "reason": "", "steps": [["root", "document", 0.9, "text"]], "v": 2,
                       "scores": {t: 0.9 for t in tags}, "ts": int(now - age)})
    for rel, sugg, reason in REVIEW:
        for e in report:
            if e["path"].endswith(rel):
                e.update(outcome="review", suggested=list(sugg), scores=dict(sugg), reason=reason)
                if reason:
                    e["steps"] = []
    for rel, sugg in GENERIC_HIDDEN:
        f = p["files"] / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(f"generic log-like output from run {rel[-6:-4]}: {'x' * (9 * int(rel[-6:-4]))} " + "\n" * 2 + f"batch {int(rel[-6:-4]) * 37}\n")
        st = os.stat(f)
        report.append({"path": str(f), "ref": [st.st_ino, st.st_mtime_ns, st.st_size], "outcome": "review", "tags": [], "suggested": sugg,
                       "reason": "", "steps": [["root", "log", 0.55, "text"]], "v": 2, "scores": {}, "ts": int(now)})
    (p["state"] / "report.jsonl").write_text("".join(json.dumps(e) + "\n" for e in report))
    # one earlier audit verdict set, so precision chips have something to show
    verdicts = [("Taxes/receipt-printing-supplies.txt", "invoice", "ok"), ("Taxes/receipt-bakker-hardware.txt", "invoice", "ok"),
                ("Projects/roadmap-q4.md", "meeting-notes", "ok"), ("Projects/standup-2026-10-01.md", "meeting-notes", "ok"),
                ("Printing/extrusion-notes.md", "research", "wrong"), ("Printing/pla-datasheet.txt", "3d-printing", "ok"),
                ("Career/interview-transcript-orbit-labs.txt", "transcript", "ok")]
    (p["state"] / "verdicts.jsonl").write_text("".join(json.dumps({"path": str(p["files"] / r), "tag": t, "verdict": v, "ts": int(now - DAY)}) + "\n" for r, t, v in verdicts))
    (p["state"] / "discover.json").write_text(json.dumps(IDEAS))
    (p["state"] / "index_status.json").write_text(json.dumps({"last_run": int(now - 1800), "classified": 120, "remaining": 14380, "counts": {"act": 61, "review": 52, "undecided": 7, "stopped": 1}}))
    # a week of token usage, partly estimated, with a price on the hosted backend so a cost shows
    lines = []
    for day in range(7):
        for k in range(6):
            lines.append({"b": "default", "k": "classify", "in": 640 + 40 * k, "out": 18, "ts": int(now - day * DAY - k * 600)})
        for k in range(3):
            lines.append({"b": "hosted", "k": "classify", "in": 900 + 60 * k, "out": 40 + k, "ts": int(now - day * DAY - 7000 - k * 300), **({"est": 1} if k == 2 else {})})
        for _ in range(5):
            lines.append({"b": "default", "k": "file", "ts": int(now - day * DAY)})
    lines.append({"b": "proposer", "k": "propose", "in": 1500, "out": 60, "ts": int(now - 3 * DAY)})
    (p["state"] / "usage.jsonl").write_text("".join(json.dumps(l) + "\n" for l in lines))
    cfg = {
        "backends": {
            "default": {"type": "demo", "endpoint": "http://127.0.0.1:8791", "model": "demo-classifier"},
            "hosted": {"type": "demo", "endpoint": "https://api.example-classifier.test", "model": "jev-latest", "consent": True,
                       "api_key_keyring": "hosted", "price": USAGE_PRICES},
        },
        "proposer": {"type": "ollama", "endpoint": "http://127.0.0.1:11434", "model": "gemma4:e2b"},
        "dirs": [str(p["files"])], "ignore": ["node_modules", "*.bak"], "write_xattrs": True, "vocabulary": VOCABULARY,
        "schedule": {"index": "1h", "discover": "weekly"}, "tag_map": {"invoices": "invoice"},
    }
    cfgmod.config_dir(env).mkdir(parents=True, exist_ok=True, mode=0o700)
    (p["config"] / "config.json").write_text(json.dumps(cfg, indent=2) + "\n")
    return p["files"]
