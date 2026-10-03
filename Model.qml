import QtQuick
import Quickshell
import Quickshell.Io

// Sift's data layer: runs the `sift` CLI and keeps its JSON answers as properties. No visuals; the view (Content.qml) and the bar
// host (Panel.qml) both read from it, and the render harness can drive it with demo data.
Scope {
  id: root

  property string script: Qt.resolvedUrl("bin/sift").toString().replace(/^file:\/\//, "")
  property string langOverride: ""             // the render harness forces a language; normally the CLI follows config and system
  property bool opened: false                  // set by the host: the popup is visible, so refresh faster
  property var cliEnv: ({})                    // extra environment for every CLI call (the harness uses it for demo mode and language)

  // ---- translations ------------------------------------------------------------------------------------------------
  property var strings: ({})
  property string language: "en"
  property bool rtl: false
  property bool _i18nAgain: false
  property bool demo: false
  property string languageSetting: "auto"
  function tr(key, args) {
    var s = root.strings[key]
    if (s === undefined) s = key
    if (args !== undefined && args !== null) {
      for (var k in args) s = s.split("{" + k + "}").join(String(args[k]))
    }
    return s
  }
  function loadI18n() {
    if (i18nProc.running) { root._i18nAgain = true; return }       // a newer request must not be lost: run it as soon as this one ends
    var cmd = [root.script, "i18n", "--json"]
    if (root.langOverride !== "") { cmd.push("--lang"); cmd.push(root.langOverride) }
    i18nProc.command = cmd
    i18nProc.running = true
  }
  function setLanguage(code) {
    if (actProc.running) return
    root._lastArgs = ["config", "set", "language", code]
    actProc.command = [root.script, "config", "set", "language", code]
    actProc.running = true
  }
  function setDemo(on) {
    if (actProc.running) return
    root._lastArgs = ["demo", on ? "on" : "off"]
    actProc.command = [root.script, "demo", on ? "on" : "off"]
    actProc.running = true
  }

  // ---- state ------------------------------------------------------------------------------------------------------
  property string tab: "review"                // review | tags | settings
  property string backendState: "unknown"      // ok | degraded | unreachable | unconfigured | unknown
  property string backendError: ""
  property int filesSeen: 0
  property int pending: 0                      // review items that matter
  property int hidden: 0                       // generic guesses kept out of sight
  property int ideaCount: 0
  property int indexRemaining: -1
  property bool autoTag: false
  property bool showHidden: false
  property var items: []
  property var ideas: []
  property var tagRows: []
  property var merges: []
  property var auditItems: []
  property var auditRows: []
  property var folders: []
  property var ignores: []
  property string scheduleIndex: "off"
  property bool busy: false
  property bool scanning: false
  property var scanRun: null                   // progress of whatever scan is running now, started by the timer or by us
  readonly property bool scanActive: root.scanning || root.scanRun !== null
  property string lastMessage: ""
  property int selectedIndex: 0
  property string editingTag: ""
  property string addingTo: ""                 // path of the review row whose own-tag editor is open
  property string deletingTag: ""
  property var backends: []
  property var usage: ({})
  property var confirm: null           // { title, body, args, reload } while waiting for the user to confirm a destructive change
  property var _pendingConfirm: null
  property bool canUndo: false
  property string undoPath: ""               // the file the Undo button puts back (only that file)
  property var _lastArgs: []
  property string previewPath: ""

  readonly property bool isError: backendState === "unreachable" || backendState === "unconfigured"
  readonly property int attention: pending + ideaCount

  // ---- data loading -------------------------------------------------------------------------------------------------
  // The data of the visible tab. Separate from the status poll so that switching tabs while a status call is still running
  // (the first open, a quick click) never leaves the new tab empty.
  function loadTabData() {
    if (!root.opened) return
    if (root.tab === "audit") loadAudit()
    if (root.tab === "tags") loadTags()
    if (root.tab === "settings") loadSettings()
  }

  function refresh() {
    root.loadTabData()
    if (statusProc.running) return
    busy = true
    statusProc.command = [root.script, "status", "--json"]; statusProc.running = true
    if (!reviewProc.running) {
      var rc = [root.script, "review", "--json", "--limit", "40"]
      if (root.showHidden) rc.push("--all")
      reviewProc.command = rc; reviewProc.running = true
    }
    if (!ideasProc.running) { ideasProc.command = [root.script, "discover", "list", "--json"]; ideasProc.running = true }
  }

  function loadAudit() {
    if (!auditProc.running) { auditProc.command = [root.script, "audit", "next", "--json", "-n", "6"]; auditProc.running = true }
    if (!auditReportProc.running) { auditReportProc.command = [root.script, "audit", "report", "--json"]; auditReportProc.running = true }
  }

  function preview(path) {
    if (openProc.running) return
    root.previewPath = path
    openProc.command = [root.script, "open", path]
    openProc.running = true
  }

  // Destructive changes: ask the CLI how many files would be touched (a dry run), explain it, and only then do it.
  function askConfirm(title, args, explain, reload, alwaysAsk) {
    if (dryProc.running) return
    root._pendingConfirm = { title: title, args: args, explain: explain, reload: reload || "tags", alwaysAsk: !!alwaysAsk }
    dryProc.command = [root.script].concat(args).concat(["--dry-run", "--json"])
    dryProc.running = true
  }
  function confirmYes() {
    if (!root.confirm) return
    var c = root.confirm
    root.confirm = null
    root.run(c.args.concat(["--yes"]), c.reload)
  }
  function confirmNo() { root.confirm = null }
  // Reasons come from the CLI as English text; show them in the user's language.
  function reasonText(r) {
    if (!r) return ""
    if (r.indexOf("possible secret") === 0) return root.tr("reason.secret")
    if (r.indexOf("middling confidence") === 0) return root.tr("reason.middling")
    if (r.indexOf("low confidence") === 0) return root.tr("reason.low")
    if (r.indexOf("filename-only") === 0) return root.tr("reason.filename")
    return r
  }
  // What to say after an action succeeded (the CLI's own wording is for terminals).
  function successText(a) {
    var k = (a[0] || "") + " " + (a[1] || "")
    if (a[0] === "audit" && a[1] === "mark") return a[4] === "wrong" ? root.tr("msg.tagRemoved") : root.tr("msg.recorded")
    var map = { "tags delete": "msg.tagDeleted", "tags merge": "msg.tagsMerged", "tags unmerge": "msg.tagUpdated", "tags ignore-merge": "msg.dismissed", "tags edit": "msg.tagUpdated", "tags rename": "msg.tagUpdated",
                "tags add": "msg.tagAdded", "discover accept": "msg.ideaAdded", "discover reject": "msg.dismissed", "dirs add": "msg.folders", "dirs remove": "msg.folders",
                "dirs ignore": "msg.folders", "dirs unignore": "msg.folders", "schedule set": "msg.schedule", "config set": "msg.saved",
                "untag --last": "msg.restored", "demo on": "msg.demoOn", "demo off": "msg.demoOff" }
    if (a[0] === "accept" || a[0] === "add-tag") return root.tr("msg.applied")
    if (a[0] === "reject") return root.tr("msg.dismissed")
    return map[k] ? root.tr(map[k]) : ""
  }
  function fmtTokens(n) {
    if (n === null || n === undefined) return "-"
    if (n >= 1000000) return (n / 1000000).toFixed(1) + "M"
    if (n >= 1000) return (n / 1000).toFixed(1) + "k"
    return String(n)
  }

  function loadTags() {
    if (!scanTagsProc.running) { scanTagsProc.command = [root.script, "tags", "scan", "--if-older", "3600"]; scanTagsProc.running = true }
    if (!tagsProc.running) { tagsProc.command = [root.script, "tags", "list", "--json"]; tagsProc.running = true }
    if (!similarProc.running) { similarProc.command = [root.script, "tags", "similar", "--json"]; similarProc.running = true }
  }

  function startAdd(path) { root.addingTo = path; if (!tagsProc.running) loadTags() }

  function loadSettings() {
    if (!dirsProc.running) { dirsProc.command = [root.script, "dirs", "list", "--json"]; dirsProc.running = true }
    if (!schedProc.running) { schedProc.command = [root.script, "schedule", "status", "--json"]; schedProc.running = true }
  }

  function run(args, reload) {
    if (actProc.running) return
    root._lastArgs = args
    root.canUndo = false
    root._reload = reload || "all"
    actProc.command = [root.script].concat(args)
    actProc.running = true
  }
  property string _reload: "all"

  function scan() {
    if (scanProc.running) return
    if (root.scanRun !== null) {
      root.lastMessage = root.tr("msg.scanRunning", {done: root.scanRun.done, budget: root.scanRun.budget})
      return
    }
    scanning = true; lastMessage = ""
    scanProc.command = [root.script, "index", "--budget", "200", "--max-seconds", "240"]
    scanProc.running = true
    refreshSoon.restart()
  }

  function openTerminal(subcommand) {
    var quoted = "'" + String(root.script).replace(/'/g, "'\\''") + "'"           // a plugin path with spaces must stay one word
    Quickshell.execDetached(["omarchy-launch-floating-terminal-with-presentation", quoted + " " + subcommand])
  }

  onOpenedChanged: { if (root.opened) root.refresh() }
  onTabChanged: { root.loadTabData(); if (root.opened) root.refresh() }
  onShowHiddenChanged: { root.reviewProc_reload() }
  function reviewProc_reload() { if (!reviewProc.running) { root.refresh() } }
  Component.onCompleted: { root.loadI18n(); root.refresh() }

  Timer { id: refreshSoon; interval: 1500; onTriggered: root.refresh() }
  Timer {
    interval: root.scanActive ? 4000 : (root.opened ? 15000 : 120000)
    repeat: true
    running: true
    onTriggered: root.refresh()
  }

  // One failed read must be visible: the old data stays, and the footer says which part could not be loaded and why.
  property string loadError: ""
  property string loadErrorWhat: ""
  component JsonProc: Process {
    id: jp
    property string what: ""
    environment: root.cliEnv
    signal parsed(var data)
    signal failed(string why)
    stdout: StdioCollector { id: collector; waitForEnd: true }
    stderr: StdioCollector { id: errCollector; waitForEnd: true }
    onExited: function(exitCode) {
      if (exitCode !== 0) {
        var why = String(errCollector.text).trim().split("\n").pop() || ("exit " + exitCode)
        root.loadErrorWhat = jp.what
        root.loadError = root.tr("msg.loadFailed", {what: jp.what, err: why.substring(0, 160)})
        jp.failed(why)
        return
      }
      try { jp.parsed(JSON.parse(String(collector.text || "null"))); if (root.loadErrorWhat === jp.what) { root.loadError = ""; root.loadErrorWhat = "" } }
      catch (e) { root.loadErrorWhat = jp.what; root.loadError = root.tr("msg.loadFailed", {what: jp.what, err: "unreadable answer"}); console.warn("sift: bad JSON", e) }
    }
  }

  JsonProc {
    id: statusProc
    what: "status"
    onFailed: function(why) { root.busy = false; root.backendState = "unreachable"; root.backendError = why.substring(0, 120) }
    onParsed: function(s) {
      root.busy = false
      if (!s) return
      root.demo = !!s.demo
      root.backendState = s.backend ? s.backend.state : "unknown"
      root.backendError = s.backend && s.backend.error ? s.backend.error : ""
      root.filesSeen = s.files || 0
      root.pending = s.pending || 0
      root.hidden = s.hidden || 0
      root.ideaCount = s.ideas || 0
      root.autoTag = !!s.write_xattrs
      root.scanRun = s.scan || null
      root.backends = s.backends || []
      root.usage = s.usage || ({})
      root.indexRemaining = (s.index && s.index.remaining !== undefined) ? s.index.remaining : -1
    }
    onExited: function(exitCode) { root.busy = false; if (exitCode !== 0) { root.backendState = "unreachable"; root.backendError = root.tr("msg.statusFailed") } }
  }
  JsonProc {
    id: reviewProc
    what: "review"
    onParsed: function(d) {
      root.items = d || []
      if (root.selectedIndex >= root.items.length) root.selectedIndex = Math.max(0, root.items.length - 1)
    }
  }
  JsonProc {
    id: i18nProc
    onExited: function(exitCode) { if (root._i18nAgain) { root._i18nAgain = false; root.loadI18n() } }
    onParsed: function(d) { if (d && d.strings) { root.strings = d.strings; root.language = d.lang || "en"; root.rtl = d.dir === "rtl"; root.languageSetting = d.setting || "auto" } }
  }
  JsonProc { id: auditProc; what: "audit"; onParsed: function(d) { root.auditItems = d || [] } }
  JsonProc { id: auditReportProc; what: "audit report"; onParsed: function(d) { root.auditRows = d || [] } }
  Process {
    id: openProc
    environment: root.cliEnv
    stderr: StdioCollector { id: openErr; waitForEnd: true }
    onExited: function(exitCode) {
      root.previewPath = ""
      if (exitCode !== 0) root.lastMessage = String(openErr.text).trim().split("\n")[0]
    }
  }
  JsonProc {
    id: dryProc
    onParsed: function(d) {
      var p = root._pendingConfirm
      root._pendingConfirm = null
      if (!p || !d) return
      var n = d.files || 0
      if (n === 0 && !p.alwaysAsk) { root.run(p.args.concat(["--yes"]), p.reload); return }   // nothing on disk changes: no need to ask
      root.confirm = { title: p.title, body: p.explain.replace("{n}", n), args: p.args, reload: p.reload }
    }
  }
  Timer { id: undoTimer; interval: 20000; onTriggered: root.canUndo = false }
  JsonProc { id: ideasProc; what: "ideas"; onParsed: function(d) { root.ideas = d || [] } }
  JsonProc { id: tagsProc; what: "tags"; onParsed: function(d) { root.tagRows = d || [] } }
  JsonProc { id: scanTagsProc; what: "tags"; onParsed: function(d) { if (!tagsProc.running) { tagsProc.command = [root.script, "tags", "list", "--json"]; tagsProc.running = true } } }
  JsonProc { id: similarProc; what: "merges"; onParsed: function(d) { root.merges = d || [] } }
  JsonProc {
    id: dirsProc
    what: "folders"
    onParsed: function(d) { if (d) { root.folders = d.dirs || []; root.ignores = d.ignore || [] } }
  }
  JsonProc {
    id: schedProc
    what: "schedule"
    onParsed: function(d) { if (d && d.schedule) root.scheduleIndex = d.schedule.index || "off" }
  }

  Process {
    id: actProc
    environment: root.cliEnv
    stdout: StdioCollector { id: actOut; waitForEnd: true }
    stderr: StdioCollector { id: actErr; waitForEnd: true }
    onExited: function(exitCode) {
      root.lastMessage = exitCode === 0 ? root.successText(root._lastArgs) : String(actErr.text).trim().split("\n")[0]
      if (exitCode === 0) { root.editingTag = ""; root.deletingTag = ""; root.addingTo = "" }      // on an error keep the editor open so the text is not lost
      var a = root._lastArgs
      if (a.length >= 2 && (a[0] === "demo" || (a[0] === "config" && a[2] === "language"))) root.loadI18n()
      if (exitCode === 0 && a.length >= 5 && a[0] === "audit" && a[1] === "mark" && a[4] === "wrong" && String(actOut.text).indexOf("pruned") !== -1) { root.undoPath = a[2]; root.canUndo = true; undoTimer.restart() }
      root.refresh()
    }
  }

  Process {
    id: scanProc
    environment: root.cliEnv
    onExited: function(exitCode) {
      root.scanning = false
      root.lastMessage = exitCode === 0 ? root.tr("msg.scanFinished") : (exitCode === 4 ? root.tr("msg.scanAlready") : root.tr("msg.scanFailed"))
      root.refresh()
    }
  }

}
