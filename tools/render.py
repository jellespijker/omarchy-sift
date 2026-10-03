#!/usr/bin/env python3
"""Render the real panel view in a throwaway Quickshell window and save PNGs of just its content.

It cycles themes, languages and tabs inside one process, runs against demo data (nothing private appears), and never touches the
user's real theme: a patched copy of the shell's Commons points at the theme folder being rendered.

    tools/render.py --out /tmp/shots --themes nord,gruvbox --langs en,de --tabs review,tags
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHELL = Path("/usr/share/omarchy/shell")
THEMES = [Path("/usr/share/omarchy/themes"), Path.home() / ".config/omarchy/themes"]

HARNESS = r'''
import QtQuick
import Quickshell
import qs.Commons
import qs.Ui

ShellRoot {
  id: harness
  property var jobs: JSON.parse(Quickshell.env("SIFT_H_JOBS"))
  property int idx: -1
  readonly property int pad: Style.space(12)

  Model {
    id: model
    opened: true
    script: Quickshell.env("SIFT_H_BIN")
    cliEnv: ({ SIFT_DEMO: "1", SIFT_DEMO_HOME: Quickshell.env("SIFT_DEMO_HOME"), SIFT_DEMO_SCAN: Quickshell.env("SIFT_H_SCAN") })
  }

  FloatingWindow {
    id: win
    title: "sift-harness"
    visible: true
    implicitWidth: Style.space(560)
    implicitHeight: Math.max(Style.space(300), card.height + Style.space(24))
    color: Color.background

    Rectangle {
      id: card
      x: Style.space(12); y: Style.space(12)
      width: Style.space(480) + 2 * harness.pad
      height: content.implicitHeight + 2 * harness.pad
      color: Color.popups.background
      border.color: Color.popups.border
      border.width: 1
      radius: Style.cornerRadius
      Content {
        id: content
        m: model
        x: harness.pad; y: harness.pad
        width: Style.space(480)
      }
    }
  }

  function apply(job) {
    Color.currentThemePath = job.theme
    model.langOverride = job.lang
    model.loadI18n()
    model.confirm = null
    model.editingTag = job.editing ? job.editing : ""
    model.addingTo = ""
    model.showHidden = !!job.showHidden
    model.tab = job.tab
    model.refresh()
  }

  Timer { id: quitTimer; interval: 800; onTriggered: Qt.quit() }

  // One state at a time: apply it, let the CLI answers arrive, capture, and only then move on.
  function next() {
    harness.idx += 1
    if (harness.idx >= harness.jobs.length) { quitTimer.start(); return }
    harness.apply(harness.jobs[harness.idx])
    settle.interval = harness.idx === 0 ? 5000 : 2200
    settle.restart()
  }
  Timer {
    id: settle
    onTriggered: {
      var job = harness.jobs[harness.idx]
      if (job.confirm && !model.confirm) {
        model.confirm = { title: model.tr("tags.deleteTitle", {tag: "invoice"}), body: model.tr("tags.deleteBody").split("{n}").join("12"), args: [], reload: "tags" }
        settle.interval = 400; settle.restart(); return
      }
      if (job.adding && model.addingTo === "" && model.items.length > 0) {
        model.startAdd(model.items[0].path)
        settle.interval = 900; settle.restart(); return
      }
      console.log("CONTRAST " + JSON.stringify({ theme: job.theme.split("/").pop(), dim: content.contrast(content.dim, content.rowFill), fg: content.contrast(content.foreground, content.rowFill),
        accentIcon: content.contrast(content.accentIcon, content.panelBg), urgent: content.contrast(content.urgentText, content.urgentFill),
        okText: content.contrast(content.okText, content.rowFill), raw_muted: content.contrast(Color.muted, content.rowFill) }))
      card.grabToImage(function(r) { r.saveToFile(job.out); harness.next() })
    }
  }
  Component.onCompleted: Qt.callLater(harness.next)
}
'''


def theme_dir(name: str) -> Path | None:
    for base in THEMES:
        if (base / name / "colors.toml").exists():
            return base / name
    return None


def build(workdir: Path) -> None:
    shutil.copytree(SHELL / "Commons", workdir / "Commons")
    color = workdir / "Commons" / "Color.qml"
    src = color.read_text()
    patched = src.replace('readonly property string currentThemePath: stateHome + "/omarchy/current/theme"',
                          'property string currentThemePath: Quickshell.env("SIFT_H_THEME") || (stateHome + "/omarchy/current/theme")')
    assert patched != src, "Color.qml changed upstream: update the harness patch"
    color.write_text(patched)
    (workdir / "Ui").symlink_to(SHELL / "Ui")
    for f in ("Model.qml", "Content.qml"):
        (workdir / f).symlink_to(ROOT / f)
    (workdir / "shell.qml").write_text(HARNESS)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--themes", default="current", help="comma list, 'all', or 'current'")
    ap.add_argument("--langs", default="en"); ap.add_argument("--tabs", default="review,audit,tags,settings")
    ap.add_argument("--demo-home", help="where the invented demo files live (shown in paths); default is a temporary folder")
    ap.add_argument("--scan", action="store_true", help="show the 'scan running' state")
    ap.add_argument("--extra", action="store_true", help="also render the confirmation card and the tag editor")
    a = ap.parse_args()
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="sift-harness-"))
    demo_home = Path(a.demo_home) if a.demo_home else work / "demo"
    build(work)
    env = {**os.environ, "SIFT_DEMO": "1", "SIFT_DEMO_HOME": str(demo_home)}
    subprocess.run([str(ROOT / "bin" / "sift"), "demo", "reset"], env=env, check=True, capture_output=True)
    names: list[str] = []
    if a.themes == "all":
        names = sorted({p.name for b in THEMES if b.exists() for p in b.iterdir() if (p / "colors.toml").exists()})
    elif a.themes == "current":
        names = ["current"]
    else:
        names = a.themes.split(",")
    jobs = []
    tdirs: dict[str, str] = {}
    for n in names:
        if n == "current":
            tdirs[n] = str(Path.home() / ".local/state/omarchy/current/theme")
        else:
            d = theme_dir(n)
            if not d:
                print(f"unknown theme {n}", file=sys.stderr); continue
            tdirs[n] = str(d)
    for n, d in tdirs.items():
        for lang in a.langs.split(","):
            for tab in a.tabs.split(","):
                jobs.append({"theme": d, "lang": lang, "tab": tab, "out": str(out / f"{n}__{lang}__{tab}.png")})
    if a.extra:
        n0, d0 = next(iter(tdirs.items()))
        for lang in a.langs.split(","):
            jobs.append({"theme": d0, "lang": lang, "tab": "tags", "editing": "invoice", "out": str(out / f"{n0}__{lang}__tags-edit.png")})
            jobs.append({"theme": d0, "lang": lang, "tab": "tags", "out": str(out / f"{n0}__{lang}__confirm.png"),
                         "confirm": True})
            jobs.append({"theme": d0, "lang": lang, "tab": "review", "adding": True, "out": str(out / f"{n0}__{lang}__review-own-tag.png")})
            jobs.append({"theme": d0, "lang": lang, "tab": "review", "showHidden": True, "out": str(out / f"{n0}__{lang}__review-all.png")})
    env.update(SIFT_H_JOBS=json.dumps(jobs), SIFT_H_BIN=str(ROOT / "bin" / "sift"), SIFT_H_THEME=next(iter(tdirs.values())),
               SIFT_H_SCAN="1" if a.scan else "")
    print(f"rendering {len(jobs)} screens ...", file=sys.stderr)
    r = subprocess.run(["quickshell", "-n", "-p", str(work)], env=env, timeout=60 + 3 * len(jobs), capture_output=True, text=True)
    for line in (r.stdout + r.stderr).splitlines():
        if "CONTRAST " in line:
            print(line[line.index("CONTRAST "):])
    if r.returncode != 0 or os.environ.get("SIFT_H_DEBUG"):
        print((r.stdout + r.stderr)[-2500:], file=sys.stderr)
    done = sum(1 for j in jobs if Path(j["out"]).exists())
    print(f"{done}/{len(jobs)} images in {out}")
    shutil.rmtree(work, ignore_errors=True)
    return 0 if done == len(jobs) else 1


if __name__ == "__main__":
    raise SystemExit(main())
