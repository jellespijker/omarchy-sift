import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import Quickshell
import qs.Commons
import qs.Ui

// Sift's popup content: Review, Audit, Tags and Settings. It only displays what the Model (Model.qml) holds and calls its functions, so
// the same view runs inside the bar popup (Panel.qml) and inside the render harness (tools/render.py).
//
// Colour rules, learned from rendering it in 31 themes: the theme's `muted` role fails 4.5:1 in 22 of them, so secondary text is
// computed from the theme's own foreground until it reaches 4.5:1 on the tinted fills it sits on; accent is decoration (underlines,
// borders), never text on an accent-tinted fill. Nothing is smaller than 11 px.
ColumnLayout {
  id: root
  property var m                                   // the Model
  property var bar: null                           // the host bar, when there is one

  // ---- colour ---------------------------------------------------------------------------------------------------------
  readonly property color panelBg: Qt.rgba(Color.popups.background.r, Color.popups.background.g, Color.popups.background.b, 1)
  readonly property color foreground: Color.popups.text
  function lum(c) {
    function f(v) { return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4) }
    return 0.2126 * f(c.r) + 0.7152 * f(c.g) + 0.0722 * f(c.b)
  }
  function contrast(a, b) {
    var la = lum(a), lb = lum(b)
    return (Math.max(la, lb) + 0.05) / (Math.min(la, lb) + 0.05)
  }
  function mix(a, b, t) { return Qt.rgba(a.r * t + b.r * (1 - t), a.g * t + b.g * (1 - t), a.b * t + b.b * (1 - t), 1) }
  // `c` moved toward the foreground until it reads at `target`:1 on `bg` (unchanged when it already does).
  function readableOn(c, bg, target) {
    var out = c
    for (var t = 0; t <= 1.0001 && contrast(out, bg) < target; t += 0.05) out = mix(root.foreground, c, t)
    return out
  }
  readonly property color rowFill: mix(root.foreground, root.panelBg, 0.12)           // the most tinted surface text sits on
  readonly property color dim: readableOn(mix(root.foreground, root.panelBg, 0.72), root.rowFill, 4.5)
  readonly property color accent: Color.accent
  readonly property color accentIcon: readableOn(Color.accent, root.panelBg, 3.0)      // graphics need 3:1
  readonly property color urgent: Color.urgent
  readonly property color urgentFill: mix(root.urgent, root.panelBg, 0.12)
  readonly property color urgentText: readableOn(root.urgent, root.urgentFill, 4.5)
  readonly property color okText: readableOn(Color.accent, root.rowFill, 4.5)

  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family
  readonly property real small: Math.max(Style.font ? Style.font.bodySmall : 11, 11)
  readonly property real body: Style.font ? Style.font.body : 12

  readonly property string iconTag: "󰓹"
  readonly property string iconCheck: "󰄬"
  readonly property string iconClose: "󰅖"
  readonly property string iconRefresh: "󰑐"
  readonly property string iconScan: "󰉋"
  readonly property string iconEdit: "󰏫"
  readonly property string iconEye: "󰈈"

  property bool ideasOpen: false
  property var openDetails: ({})                   // backend name -> details expanded
  property bool usageOpen: false

  LayoutMirroring.enabled: m.rtl                   // Arabic: the whole layout mirrors
  LayoutMirroring.childrenInherit: true
  spacing: Style.space(8)

  function moveSelection(dy) {
    if (m.tab !== "review" || dy === 0 || m.items.length === 0) return
    m.selectedIndex = Math.max(0, Math.min(m.items.length - 1, m.selectedIndex + dy))
    reviewList.positionViewAtIndex(m.selectedIndex, ListView.Contain)
  }
  function acceptSelected() {
    if (m.tab === "review" && m.items.length > m.selectedIndex && m.items[m.selectedIndex].suggested.length > 0)
      m.run(["accept", m.items[m.selectedIndex].path])
  }
  function rejectSelected() {
    if (m.tab === "review" && m.items.length > m.selectedIndex) m.run(["reject", m.items[m.selectedIndex].path])
  }

  // ---- small building blocks -------------------------------------------------------------------------------------------------
  // Equal-width choices that shrink and elide, so a longer translation can never push the row past the panel edge.
  component Segmented: RowLayout {
    id: seg
    property var options: []                       // [{ value, label }]
    property string value: ""
    signal picked(string v)
    spacing: Style.space(4)
    Repeater {
      model: seg.options
      delegate: Rectangle {
        id: opt
        required property var modelData
        readonly property bool selected: seg.value === opt.modelData.value
        Layout.fillWidth: true
        Layout.preferredWidth: 1
        implicitHeight: Style.space(32)
        radius: Style.space(4)
        color: opt.selected ? root.rowFill : (optMouse.containsMouse ? Util.alpha(root.foreground, 0.06) : "transparent")
        border.color: opt.selected ? Util.alpha(root.foreground, 0.28) : Util.alpha(root.foreground, 0.14)
        border.width: 1
        Accessible.role: Accessible.RadioButton
        Accessible.name: opt.modelData.label
        Accessible.checked: opt.selected
        Text {
          anchors.fill: parent
          anchors.leftMargin: Style.space(4); anchors.rightMargin: Style.space(4)
          horizontalAlignment: Text.AlignHCenter
          verticalAlignment: Text.AlignVCenter
          elide: Text.ElideRight
          text: opt.modelData.label
          color: root.foreground
          font.family: root.fontFamily
          font.pixelSize: root.body
          font.bold: opt.selected
        }
        Rectangle {                                // the selected option is underlined in the accent colour
          visible: opt.selected
          anchors.bottom: parent.bottom
          anchors.left: parent.left; anchors.right: parent.right
          anchors.leftMargin: Style.space(8); anchors.rightMargin: Style.space(8)
          height: 2
          radius: 1
          color: root.accentIcon
        }
        MouseArea {
          id: optMouse
          anchors.fill: parent
          hoverEnabled: true
          cursorShape: Qt.PointingHandCursor
          onClicked: seg.picked(opt.modelData.value)
        }
      }
    }
  }

  // A tag or action pill that never grows wider than the panel.
  component Chip: Rectangle {
    id: chip
    property string label: ""
    property bool clickable: true
    property bool accented: false
    property string hint: ""
    signal clicked()
    implicitHeight: Style.space(28)
    implicitWidth: Math.min(chipText.implicitWidth + Style.space(18), Style.space(440))
    radius: Style.space(4)
    color: chipMouse.containsMouse && chip.clickable ? Util.alpha(root.accent, 0.20) : Util.alpha(root.foreground, 0.08)
    border.color: chip.accented ? root.accentIcon : Util.alpha(root.foreground, 0.20)
    border.width: 1
    Accessible.role: chip.clickable ? Accessible.Button : Accessible.StaticText
    Accessible.name: chip.hint !== "" ? chip.hint : chip.label
    Text {
      id: chipText
      anchors.fill: parent
      anchors.leftMargin: Style.space(9); anchors.rightMargin: Style.space(9)
      verticalAlignment: Text.AlignVCenter
      horizontalAlignment: Text.AlignHCenter
      elide: Text.ElideRight
      text: chip.label
      color: root.foreground
      font.family: root.fontFamily
      font.pixelSize: root.small
    }
    MouseArea {
      id: chipMouse
      anchors.fill: parent
      enabled: chip.clickable
      hoverEnabled: true
      cursorShape: Qt.PointingHandCursor
      onClicked: chip.clicked()
    }
  }

  component Hint: Text {
    Layout.fillWidth: true
    horizontalAlignment: m.rtl ? Text.AlignRight : Text.AlignLeft
    wrapMode: Text.WordWrap
    color: root.dim
    font.family: root.fontFamily
    font.pixelSize: root.small
  }

  // ---- header -------------------------------------------------------------------------------------------------------------------
  PanelHero {
    Layout.fillWidth: true
    title: "Sift"
    meta: m.isError ? m.tr("hero.error", {state: m.backendState})
                    : (m.pending > 0 ? m.tr("hero.pending", {n: m.pending}) : m.tr("hero.quiet"))
    detail: m.tr("hero.files", {n: m.filesSeen})
    foreground: root.foreground
    fontFamily: root.fontFamily
    iconOpacity: 1.0
    iconComponent: Component {
      Text {
        text: root.iconTag
        font.family: root.fontFamily
        font.pixelSize: Style.space(24)
        color: m.isError ? root.urgentText : root.foreground
      }
    }
    trailingControl: Component {
      RowLayout {
        spacing: Style.space(4)
        Button {
          text: ""; iconText: root.iconScan; iconSpinning: m.scanActive
          fontFamily: root.fontFamily
          foreground: root.foreground
          accent: root.accent; bordered: false
          tooltipText: m.scanRun !== null ? m.tr("scan.runningTip", {done: m.scanRun.done, budget: m.scanRun.budget}) : m.tr("scan.now")
          Accessible.name: tooltipText
          onClicked: m.scan()
        }
        Button {
          text: ""; iconText: root.iconRefresh; iconSpinning: m.busy
          fontFamily: root.fontFamily
          foreground: root.foreground
          accent: root.accent; bordered: false
          tooltipText: m.tr("refresh")
          Accessible.name: tooltipText
          onClicked: m.refresh()
        }
      }
    }
  }

  // Scan progress: one thin bar and one short line, only while a scan runs.
  ColumnLayout {
    visible: m.scanActive
    Layout.fillWidth: true
    spacing: Style.space(3)
    Text {
      horizontalAlignment: m.rtl ? Text.AlignRight : Text.AlignLeft
      Layout.fillWidth: true
      elide: Text.ElideRight
      text: m.scanRun !== null ? m.tr("hero.scanning", {done: m.scanRun.done, budget: m.scanRun.budget}) : m.tr("scan.starting")
      color: root.foreground
      font.family: root.fontFamily
      font.pixelSize: root.small
    }
    Rectangle {
      Layout.fillWidth: true
      implicitHeight: 4
      radius: 2
      color: Util.alpha(root.foreground, 0.16)
      Rectangle {
        height: parent.height
        radius: 2
        color: root.accentIcon
        width: m.scanRun !== null && m.scanRun.budget > 0 ? parent.width * Math.min(m.scanRun.done / m.scanRun.budget, 1) : parent.width * 0.08
      }
    }
  }

  // Demo mode: a slim strip, not a banner; it should never outweigh the task.
  RowLayout {
    visible: m.demo
    Layout.fillWidth: true
    spacing: Style.space(8)
    Text {
      horizontalAlignment: m.rtl ? Text.AlignRight : Text.AlignLeft
      Layout.fillWidth: true
      text: m.tr("demo.strip")
      wrapMode: Text.WordWrap
      color: root.foreground
      font.family: root.fontFamily
      font.pixelSize: root.small
    }
    Chip { label: m.tr("demo.exit"); accented: true; onClicked: m.setDemo(false) }
  }

  Segmented {
    Layout.fillWidth: true
    options: [
      { value: "review", label: m.pending > 0 ? m.tr("tab.reviewCount", {n: m.pending}) : m.tr("tab.review") },
      { value: "audit", label: m.tr("tab.audit") },
      { value: "tags", label: m.ideaCount > 0 ? m.tr("tab.tagsCount", {n: m.ideaCount}) : m.tr("tab.tags") },
      { value: "settings", label: m.tr("tab.settings") }
    ]
    value: m.tab
    onPicked: function(v) { m.tab = v }
  }

  // ================= CONFIRMATION =================
  Rectangle {
    visible: m.confirm !== null
    Layout.fillWidth: true
    implicitHeight: confirmColumn.implicitHeight + Style.space(18)
    radius: Style.space(5)
    color: root.urgentFill
    border.color: root.urgent
    border.width: 1
    ColumnLayout {
      id: confirmColumn
      anchors.fill: parent
      anchors.margins: Style.space(9)
      spacing: Style.space(7)
      Text {
        horizontalAlignment: m.rtl ? Text.AlignRight : Text.AlignLeft
        Layout.fillWidth: true
        wrapMode: Text.WordWrap
        text: m.confirm ? m.confirm.title : ""
        color: root.foreground
        font.family: root.fontFamily
        font.pixelSize: root.body
        font.bold: true
      }
      Text {
        horizontalAlignment: m.rtl ? Text.AlignRight : Text.AlignLeft
        Layout.fillWidth: true
        wrapMode: Text.WordWrap
        text: m.confirm ? m.confirm.body : ""
        color: root.foreground
        font.family: root.fontFamily
        font.pixelSize: root.small
      }
      Flow {
        Layout.fillWidth: true
        spacing: Style.space(6)
        layoutDirection: m.rtl ? Qt.RightToLeft : Qt.LeftToRight
        Chip { label: m.tr("common.cancel"); onClicked: m.confirmNo() }
        Chip { label: m.tr("confirm.confirm"); accented: true; onClicked: m.confirmYes() }
      }
    }
  }

  // The four tabs share one scroll area: the popup stays a sensible height however long Settings or the Tags list get.
  Flickable {
    id: scroller
    Layout.fillWidth: true
    Layout.preferredHeight: Math.min(tabsColumn.implicitHeight, Style.space(520))
    contentWidth: width
    contentHeight: tabsColumn.implicitHeight
    clip: true
    boundsBehavior: Flickable.StopAtBounds
    flickableDirection: Flickable.VerticalFlick
    ScrollBar.vertical: ScrollBar { policy: scroller.contentHeight > scroller.height ? ScrollBar.AlwaysOn : ScrollBar.AlwaysOff; width: Style.space(6) }
    ColumnLayout {
      id: tabsColumn
      width: scroller.width - Style.space(10)
      spacing: Style.space(8)

      // ================= REVIEW =================
      ColumnLayout {
        visible: m.tab === "review"
        Layout.fillWidth: true
        spacing: Style.space(8)

        Text {
          horizontalAlignment: m.rtl ? Text.AlignRight : Text.AlignLeft
          visible: m.isError
          Layout.fillWidth: true
          wrapMode: Text.WordWrap
          text: m.backendError !== "" ? m.tr("review.errorDetail", {error: m.backendError}) : m.tr("review.notReachable")
          color: root.urgentText
          font.family: root.fontFamily
          font.pixelSize: root.small
        }

        Hint {
          visible: !m.isError && m.items.length === 0
          text: m.filesSeen === 0 ? m.tr("review.emptyNew") : m.tr("review.emptyDone")
        }
        Hint { visible: m.items.length > 0; text: m.tr("review.hint") }

        ListView {
          id: reviewList
          Layout.fillWidth: true
          Layout.preferredHeight: Math.min(contentHeight, Style.space(340))
          visible: m.items.length > 0
          clip: true
          spacing: Style.space(5)
          model: m.items
          delegate: Rectangle {
            id: row
            required property var modelData
            required property int index
            readonly property string meta: [ (row.modelData.count > 1 ? m.tr("row.similar", {n: row.modelData.count}) : ""),
                                              (row.modelData.chips.length === 0 && !row.modelData.reason ? m.tr("row.noSuggestion") : ""),
                                              m.reasonText(row.modelData.reason) ].filter(function(x) { return x !== "" }).join("  ·  ")
            width: reviewList.width
            height: rowColumn.implicitHeight + Style.space(16)
            radius: Style.space(5)
            color: row.index === m.selectedIndex ? root.rowFill : Util.alpha(root.foreground, 0.04)
            border.color: row.index === m.selectedIndex ? root.accentIcon : Util.alpha(root.foreground, 0.14)
            border.width: 1
            MouseArea { anchors.fill: parent; onClicked: m.selectedIndex = row.index }
            ColumnLayout {
              id: rowColumn
              anchors.fill: parent
              anchors.margins: Style.space(8)
              spacing: Style.space(6)
              RowLayout {                                // fixed action column: always the same three buttons at the same end
                Layout.fillWidth: true
                spacing: Style.space(2)
                Text {
                  horizontalAlignment: m.rtl ? Text.AlignRight : Text.AlignLeft
                  Layout.fillWidth: true
                  text: row.modelData.name
                  elide: Text.ElideMiddle
                  color: root.foreground
                  font.family: root.fontFamily
                  font.pixelSize: root.body
                  font.bold: true
                }
                Button {
                  text: ""; iconText: root.iconEye
                  iconSpinning: m.previewPath === row.modelData.path
                  enabled: m.previewPath === ""
                  fontFamily: root.fontFamily
                  horizontalPadding: Style.space(9); verticalPadding: Style.space(6)
                  foreground: root.foreground; accent: root.accent; bordered: false
                  tooltipText: m.tr("row.preview")
                  Accessible.name: tooltipText
                  onClicked: m.preview(row.modelData.path)
                }
                Button {
                  text: ""; iconText: root.iconCheck
                  fontFamily: root.fontFamily
                  horizontalPadding: Style.space(9); verticalPadding: Style.space(6)
                  foreground: root.foreground; accent: root.accent; bordered: false
                  enabled: row.modelData.suggested.length > 0
                  tooltipText: m.tr("row.acceptAll")
                  Accessible.name: tooltipText
                  onClicked: m.run(["accept", row.modelData.path])
                }
                Button {
                  text: ""; iconText: root.iconClose
                  fontFamily: root.fontFamily
                  horizontalPadding: Style.space(9); verticalPadding: Style.space(6)
                  foreground: root.foreground; accent: root.urgent; bordered: false
                  tooltipText: m.tr("row.dismiss")
                  Accessible.name: tooltipText
                  onClicked: m.run(["reject", row.modelData.path])
                }
              }
              Text {                                     // the full path: names alone are ambiguous (many files are called notes.txt)
                Layout.fillWidth: true
                horizontalAlignment: Text.AlignLeft      // mirrored by the layout in right-to-left languages, so it lines up with the name; the path stays left to right
                text: row.modelData.path
                wrapMode: Text.WrapAnywhere
                maximumLineCount: 2
                elide: Text.ElideMiddle
                color: root.dim
                font.family: root.fontFamily
                font.pixelSize: root.small
              }
              Flow {
                visible: row.modelData.chips.length > 0
                Layout.fillWidth: true
                spacing: Style.space(5)
                layoutDirection: m.rtl ? Qt.RightToLeft : Qt.LeftToRight
                Repeater {
                  model: row.modelData.chips
                  delegate: Chip {
                    required property var modelData
                    label: modelData.tag + (modelData.score > 0 ? "  " + Math.round(modelData.score * 100) + "%" : "")
                    hint: m.tr("row.applyTag", {tag: modelData.tag})
                    onClicked: m.run(["accept", row.modelData.path, "--tags", modelData.tag])
                  }
                }
                Chip {
                  visible: m.addingTo !== row.modelData.path
                  label: m.tr("row.addTag")
                  onClicked: m.startAdd(row.modelData.path)
                }
              }
              // Own tag: Enter saves, Escape cancels. A tag Sift does not know asks what it means, so it can be learned.
              ColumnLayout {
                id: ownTag
                visible: m.addingTo === row.modelData.path
                Layout.fillWidth: true
                spacing: Style.space(6)
                readonly property string typed: tagField.text.trim().toLowerCase().replace(/^-+/, "")      // never an option for the CLI
                readonly property var matches: {
                  var out = []
                  for (var i = 0; i < m.tagRows.length && out.length < 6; i++) {
                    var t = m.tagRows[i].tag
                    if (ownTag.typed === "" ? m.tagRows[i].kind === "vocabulary" : t.indexOf(ownTag.typed) !== -1) out.push(t)
                  }
                  return out
                }
                readonly property bool known: { for (var i = 0; i < m.tagRows.length; i++) if (m.tagRows[i].tag === ownTag.typed) return true; return false }
                function save() {
                  if (ownTag.typed === "") return
                  var a = ["add-tag", row.modelData.path, ownTag.typed]
                  if (!ownTag.known && descField2.text.trim() !== "") { a.push("--description=" + descField2.text.trim()) }
                  m.run(a)
                }
                TextField {
                  id: tagField
                  Layout.fillWidth: true
                  placeholderText: m.tr("row.addTagPh")
                  maximumLength: 40
                  font.family: root.fontFamily
                  font.pixelSize: root.body
                  foreground: root.foreground
                  Accessible.name: m.tr("row.addTagPh")
                  onVisibleChanged: if (visible) { text = ""; Qt.callLater(forceActiveFocus) }
                  onAccepted: ownTag.save()
                  Keys.onEscapePressed: m.addingTo = ""
                }
                Flow {
                  visible: ownTag.matches.length > 0
                  Layout.fillWidth: true
                  spacing: Style.space(5)
                  layoutDirection: m.rtl ? Qt.RightToLeft : Qt.LeftToRight
                  Repeater {
                    model: ownTag.matches
                    delegate: Chip {
                      required property var modelData
                      label: modelData
                      hint: m.tr("row.applyTag", {tag: modelData})
                      onClicked: tagField.text = modelData
                    }
                  }
                }
                TextField {
                  id: descField2
                  visible: ownTag.typed !== "" && !ownTag.known
                  Layout.fillWidth: true
                  placeholderText: m.tr("row.addDescPh")
                  maximumLength: 300
                  font.family: root.fontFamily
                  font.pixelSize: root.body
                  foreground: root.foreground
                  Accessible.name: m.tr("row.addDescPh")
                  onVisibleChanged: if (visible) text = ""
                  onAccepted: ownTag.save()
                  Keys.onEscapePressed: m.addingTo = ""
                }
                Hint { text: m.tr("row.addHint") }
                Flow {
                  Layout.fillWidth: true
                  spacing: Style.space(6)
                  layoutDirection: m.rtl ? Qt.RightToLeft : Qt.LeftToRight
                  Chip { label: m.tr("common.cancel"); onClicked: m.addingTo = "" }
                  Chip { label: m.tr("common.save"); accented: true; onClicked: ownTag.save() }
                }
              }
              Hint { visible: row.meta !== ""; text: row.meta }
            }
          }
        }

        Hint { visible: m.items.length > 0; text: m.tr("review.keys") }

        Text {
          horizontalAlignment: m.rtl ? Text.AlignRight : Text.AlignLeft
          visible: m.hidden > 0 || m.showHidden
          Layout.fillWidth: true
          wrapMode: Text.WordWrap
          text: m.showHidden ? m.tr("review.hiddenHide") : m.tr("review.hiddenShow", {n: m.hidden})
          color: root.foreground
          font.family: root.fontFamily
          font.pixelSize: root.small
          font.underline: true
          Accessible.role: Accessible.Button
          Accessible.name: text
          MouseArea {
            anchors.fill: parent
            cursorShape: Qt.PointingHandCursor
            onClicked: { m.showHidden = !m.showHidden; m.refresh() }
          }
        }
      }

      // ================= AUDIT =================
      ColumnLayout {
        visible: m.tab === "audit"
        Layout.fillWidth: true
        spacing: Style.space(8)

        Hint { text: m.tr("audit.help") }

        Flow {
          visible: m.auditRows.length > 0
          Layout.fillWidth: true
          spacing: Style.space(5)
          layoutDirection: m.rtl ? Qt.RightToLeft : Qt.LeftToRight
          Repeater {
            model: m.auditRows
            delegate: Chip {
              required property var modelData
              clickable: false
              label: modelData.precision === null ? m.tr("audit.precisionNone", {tag: modelData.tag})
                     : m.tr("audit.precision", {tag: modelData.tag, pct: Math.round(modelData.precision * 100), ok: modelData.ok, judged: modelData.judged, low: Math.round((modelData.at_least || 0) * 100)})
            }
          }
        }

        Hint { visible: m.auditItems.length === 0; text: m.tr("audit.none") }

        Repeater {
      model: m.auditItems
      delegate: Rectangle {
            id: auditRow
            required property var modelData
            Layout.fillWidth: true
        implicitHeight: auditColumn.implicitHeight + Style.space(16)
            radius: Style.space(5)
            color: Util.alpha(root.foreground, 0.04)
            border.color: Util.alpha(root.foreground, 0.14)
            border.width: 1
            ColumnLayout {
              id: auditColumn
              anchors.fill: parent
              anchors.margins: Style.space(8)
              spacing: Style.space(6)
              RowLayout {
                Layout.fillWidth: true
                spacing: Style.space(6)
                Text {
                  horizontalAlignment: m.rtl ? Text.AlignRight : Text.AlignLeft
                  Layout.fillWidth: true
                  text: auditRow.modelData.name
                  elide: Text.ElideMiddle
                  color: root.foreground
                  font.family: root.fontFamily
                  font.pixelSize: root.body
                  font.bold: true
                }
                Button {
                  text: m.previewPath === auditRow.modelData.path ? m.tr("audit.opening") : m.tr("audit.preview"); iconText: root.iconEye
                  iconSpinning: m.previewPath === auditRow.modelData.path
                  enabled: m.previewPath === ""
                  fontFamily: root.fontFamily
                  foreground: root.foreground; accent: root.accent; bordered: true
                  tooltipText: m.tr("audit.previewTip")
                  onClicked: m.preview(auditRow.modelData.path)
                }
              }
              Text {                                     // the full path: names alone are ambiguous (many files are called notes.txt)
                Layout.fillWidth: true
                horizontalAlignment: Text.AlignLeft      // mirrored by the layout in right-to-left languages, so it lines up with the name; the path stays left to right
                text: auditRow.modelData.path
                wrapMode: Text.WrapAnywhere
                maximumLineCount: 2
                elide: Text.ElideMiddle
                color: root.dim
                font.family: root.fontFamily
                font.pixelSize: root.small
              }
              Repeater {
                model: auditRow.modelData.tags
                delegate: RowLayout {
                  required property var modelData
                  Layout.fillWidth: true
                  spacing: Style.space(6)
                  Text {
                    horizontalAlignment: m.rtl ? Text.AlignRight : Text.AlignLeft
                    Layout.fillWidth: true
                    text: modelData
                    elide: Text.ElideRight
                    color: root.foreground
                    font.family: root.fontFamily
                    font.pixelSize: root.body
                  }
                  Button {
                    text: m.tr("audit.right"); iconText: root.iconCheck
                    fontFamily: root.fontFamily
                    foreground: root.foreground; accent: root.accent; bordered: true
                    onClicked: m.run(["audit", "mark", auditRow.modelData.path, modelData, "ok"])
                  }
                  Button {
                    text: m.tr("audit.wrong"); iconText: root.iconClose
                    fontFamily: root.fontFamily
                    foreground: root.foreground; accent: root.urgent; bordered: true
                    tooltipText: m.tr("audit.wrongTip")
                    onClicked: m.run(["audit", "mark", auditRow.modelData.path, modelData, "wrong"])
                  }
                }
              }
            }
          }
        }
      }

      // ================= TAGS =================
      ColumnLayout {
        visible: m.tab === "tags"
        Layout.fillWidth: true
        spacing: Style.space(8)

        // New tag ideas: one quiet row until the user opens it.
        Text {
          horizontalAlignment: m.rtl ? Text.AlignRight : Text.AlignLeft
          visible: m.ideas.length > 0
          Layout.fillWidth: true
          text: (root.ideasOpen ? "▾  " : "▸  ") + m.tr("tags.ideasToggle", {n: m.ideas.length})
          color: root.foreground
          font.family: root.fontFamily
          font.pixelSize: root.body
          font.bold: true
          Accessible.role: Accessible.Button
          Accessible.name: text
          MouseArea { anchors.fill: parent; cursorShape: Qt.PointingHandCursor; onClicked: root.ideasOpen = !root.ideasOpen }
        }
        Hint { visible: m.ideas.length > 0 && root.ideasOpen; text: m.tr("ideas.hint") }
        Flow {
          visible: m.ideas.length > 0 && root.ideasOpen
          Layout.fillWidth: true
          spacing: Style.space(6)
          layoutDirection: m.rtl ? Qt.RightToLeft : Qt.LeftToRight
          Repeater {
            model: m.ideas
            delegate: Row {
              id: ideaItem
              required property var modelData
              spacing: Style.space(2)
              Chip { label: ideaItem.modelData.tag + "  " + ideaItem.modelData.support; hint: m.tr("ideas.add", {tag: ideaItem.modelData.tag}); onClicked: m.run(["discover", "accept", ideaItem.modelData.tag]) }
              Chip { label: "×"; hint: m.tr("ideas.dismiss", {tag: ideaItem.modelData.tag}); onClicked: m.run(["discover", "reject", ideaItem.modelData.tag]) }
            }
          }
        }

        Hint { visible: m.merges.length > 0; text: m.tr("tags.similarHeader") }
        Repeater {
          model: m.merges
          delegate: Rectangle {
            id: mergeCard
            required property var modelData
            readonly property var all: [modelData.keep].concat(modelData.merge)
            property string dest: modelData.keep                 // the tag that stays
            property var off: ({})                               // tags the user unticked: left alone
            readonly property var sources: mergeCard.all.filter(function(t) { return t !== mergeCard.dest && !mergeCard.off[t] })
            Layout.fillWidth: true
            implicitHeight: mergeColumn.implicitHeight + Style.space(14)
            radius: Style.space(5)
            color: Util.alpha(root.foreground, 0.04)
            border.color: Util.alpha(root.foreground, 0.14)
            border.width: 1
            ColumnLayout {
              id: mergeColumn
              anchors.fill: parent
              anchors.margins: Style.space(7)
              spacing: Style.space(5)
              Hint { text: m.tr("tags.mergeWhich") }
              Flow {
                Layout.fillWidth: true
                spacing: Style.space(5)
                layoutDirection: m.rtl ? Qt.RightToLeft : Qt.LeftToRight
                Repeater {
                  model: mergeCard.all
                  delegate: Chip {
                    required property var modelData
                    readonly property bool isDest: modelData === mergeCard.dest
                    label: (isDest || !mergeCard.off[modelData] ? "✓ " : "") + modelData
                    accented: !isDest && !mergeCard.off[modelData]
                    hint: m.tr("tags.mergeToggle", {tag: modelData})
                    onClicked: {
                      if (isDest) return
                      var o = Object.assign({}, mergeCard.off); o[modelData] = !o[modelData]; mergeCard.off = o
                    }
                  }
                }
              }
              Hint { text: m.tr("tags.mergeInto") }
              Flow {
                Layout.fillWidth: true
                spacing: Style.space(5)
                layoutDirection: m.rtl ? Qt.RightToLeft : Qt.LeftToRight
                Repeater {
                  model: mergeCard.all
                  delegate: Chip {
                    required property var modelData
                    label: modelData
                    accented: modelData === mergeCard.dest
                    hint: m.tr("tags.mergeKeep", {tag: modelData})
                    onClicked: { mergeCard.dest = modelData; var o = Object.assign({}, mergeCard.off); o[modelData] = false; mergeCard.off = o }
                  }
                }
              }
              Flow {
                Layout.fillWidth: true
                spacing: Style.space(6)
                layoutDirection: m.rtl ? Qt.RightToLeft : Qt.LeftToRight
                Chip { label: m.tr("tags.mergeIgnore"); hint: m.tr("tags.mergeIgnoreHint"); onClicked: m.run(["tags", "ignore-merge"].concat(mergeCard.all), "tags") }
                Chip {
                  label: m.tr("tags.mergeDo", {n: mergeCard.sources.length})
                  accented: true
                  clickable: mergeCard.sources.length > 0
                  opacity: mergeCard.sources.length > 0 ? 1 : 0.5
                  onClicked: if (mergeCard.sources.length > 0) m.askConfirm(m.tr("tags.mergeTitle", {from: mergeCard.sources.join(", "), to: mergeCard.dest}),
                    ["tags", "merge"].concat(mergeCard.sources).concat(["--to=" + mergeCard.dest]), m.tr("tags.mergeBody", {to: mergeCard.dest}))
                }
              }
            }
          }
        }

        Repeater {
          model: m.tagRows
          delegate: Rectangle {
            id: tagRow
            required property var modelData
            readonly property bool editing: m.editingTag === tagRow.modelData.tag
            readonly property bool canDescribe: tagRow.modelData.kind === "vocabulary"
            Layout.fillWidth: true
            implicitHeight: tagColumn.implicitHeight + Style.space(14)
            radius: Style.space(5)
            color: tagRow.editing ? root.rowFill : Util.alpha(root.foreground, 0.04)
            border.color: tagRow.editing ? root.accentIcon : Util.alpha(root.foreground, 0.14)
            border.width: 1

            function save() {
              var args = ["tags", "edit", tagRow.modelData.tag]
              var changed = false
              if (tagRow.canDescribe && descField.text !== tagRow.modelData.description) { args.push("--desc=" + descField.text); changed = true }
              var renaming = nameField.text.length > 0 && nameField.text !== tagRow.modelData.tag
              if (renaming) { args.push("--to=" + nameField.text); changed = true }
              if (!changed) { m.editingTag = ""; return }
              if (renaming) m.askConfirm(m.tr("tags.renameTitle", {from: tagRow.modelData.tag, to: nameField.text}), args, m.tr("tags.renameBody"), "tags")
              else m.run(args, "tags")
            }

            ColumnLayout {
              id: tagColumn
              anchors.fill: parent
              anchors.margins: Style.space(7)
              spacing: Style.space(6)
              RowLayout {
                Layout.fillWidth: true
                spacing: Style.space(2)
                ColumnLayout {
                  visible: !tagRow.editing
                  Layout.fillWidth: true
                  spacing: 0
                  Text {
                    horizontalAlignment: m.rtl ? Text.AlignRight : Text.AlignLeft
                    Layout.fillWidth: true
                    text: tagRow.modelData.tag + (tagRow.modelData.aliases.length > 0 ? "   + " + tagRow.modelData.aliases.join(", ") : "")
                    elide: Text.ElideRight
                    color: root.foreground
                    font.family: root.fontFamily
                    font.pixelSize: root.body
                    font.bold: true
                  }
                  Text {
                    horizontalAlignment: m.rtl ? Text.AlignRight : Text.AlignLeft
                    Layout.fillWidth: true
                    text: (tagRow.modelData.kind === "yours" ? m.tr("tags.yours") + "  ·  " : "") + m.tr("tags.files", {n: tagRow.modelData.files})
                    elide: Text.ElideRight
                    color: root.dim
                    font.family: root.fontFamily
                    font.pixelSize: root.small
                  }
                  Flow {                                     // merged tags: one click brings a merged tag back on its own
                    visible: tagRow.modelData.aliases.length > 0
                    Layout.fillWidth: true
                    Layout.topMargin: Style.space(4)
                    spacing: Style.space(5)
                    layoutDirection: m.rtl ? Qt.RightToLeft : Qt.LeftToRight
                    Repeater {
                      model: tagRow.modelData.aliases
                      delegate: Chip {
                        required property var modelData
                        label: modelData + "  ×"
                        hint: m.tr("tags.unmerge", {tag: modelData, into: tagRow.modelData.tag})
                        onClicked: m.askConfirm(m.tr("tags.unmergeTitle", {tag: modelData, into: tagRow.modelData.tag}), ["tags", "unmerge", modelData],
                                                m.tr("tags.unmergeBody", {tag: modelData}), "tags", true)
                      }
                    }
                  }
                }
                Item { visible: tagRow.editing; Layout.fillWidth: true }
                Button {
                  visible: !tagRow.editing
                  text: ""; iconText: root.iconEdit
                  fontFamily: root.fontFamily
                  horizontalPadding: Style.space(9); verticalPadding: Style.space(6)
                  foreground: root.foreground; accent: root.accent; bordered: false
                  tooltipText: m.tr("tags.edit")
                  Accessible.name: tooltipText
                  onClicked: m.editingTag = tagRow.modelData.tag
                }
                Button {
                  visible: !tagRow.editing
                  text: ""; iconText: root.iconClose
                  fontFamily: root.fontFamily
                  horizontalPadding: Style.space(9); verticalPadding: Style.space(6)
                  foreground: root.foreground; accent: root.urgent; bordered: false
                  tooltipText: m.tr("tags.delete")
                  Accessible.name: tooltipText
                  onClicked: {
                    m.editingTag = ""
                    m.askConfirm(m.tr("tags.deleteTitle", {tag: tagRow.modelData.tag}), ["tags", "delete", tagRow.modelData.tag], m.tr("tags.deleteBody"), "tags", true)
                  }
                }
              }

              // Editor: Enter in either field saves, Escape cancels.
              ColumnLayout {
                visible: tagRow.editing
                Layout.fillWidth: true
                spacing: Style.space(6)
                TextField {
                  id: nameField
                  Layout.fillWidth: true
                  text: tagRow.modelData.tag
                  placeholderText: m.tr("tags.namePh")
                  maximumLength: 40
                  font.family: root.fontFamily
                  font.pixelSize: root.body
                  foreground: root.foreground
                  Accessible.name: m.tr("tags.namePh")
                  onVisibleChanged: if (visible) { text = tagRow.modelData.tag; Qt.callLater(forceActiveFocus); Qt.callLater(selectAll) }
                  onAccepted: tagRow.save()
                  Keys.onEscapePressed: m.editingTag = ""
                }
                TextField {
                  id: descField
                  Layout.fillWidth: true
                  enabled: tagRow.canDescribe
                  text: tagRow.modelData.description
                  placeholderText: tagRow.canDescribe ? m.tr("tags.descPh") : m.tr("tags.descBuiltin")
                  maximumLength: 300
                  font.family: root.fontFamily
                  font.pixelSize: root.body
                  foreground: root.foreground
                  Accessible.name: m.tr("tags.descPh")
                  onVisibleChanged: if (visible) text = tagRow.modelData.description
                  onAccepted: tagRow.save()
                  Keys.onEscapePressed: m.editingTag = ""
                }
                Hint { text: m.tr("tags.editHint") }
                Flow {
                  Layout.fillWidth: true
                  spacing: Style.space(6)
                  layoutDirection: m.rtl ? Qt.RightToLeft : Qt.LeftToRight
                  Chip { label: m.tr("common.cancel"); onClicked: m.editingTag = "" }
                  Chip { label: m.tr("common.save"); accented: true; onClicked: tagRow.save() }
                }
              }
            }
          }
        }

        // Add a tag: two stacked fields, so neither is squeezed.
        ColumnLayout {
          Layout.fillWidth: true
          spacing: Style.space(6)
          TextField {
            id: newTagName
            Layout.fillWidth: true
            maximumLength: 40
            placeholderText: m.tr("tags.newPh")
            font.family: root.fontFamily
            font.pixelSize: root.body
            foreground: root.foreground
            Accessible.name: m.tr("tags.newPh")
          }
          TextField {
            id: newTagDesc
            Layout.fillWidth: true
            maximumLength: 300
            placeholderText: m.tr("tags.newDescPh")
            font.family: root.fontFamily
            font.pixelSize: root.body
            foreground: root.foreground
            Accessible.name: m.tr("tags.newDescPh")
            onAccepted: addTag.clicked()
          }
          Flow {
            Layout.fillWidth: true
            layoutDirection: m.rtl ? Qt.LeftToRight : Qt.RightToLeft
            Chip {
              id: addTag
              label: m.tr("common.add"); accented: true
              onClicked: {
                if (newTagName.text.length > 0 && newTagDesc.text.length > 0) {
                  m.run(["tags", "add", String(newTagName.text).replace(/^-+/, ""), "--desc=" + newTagDesc.text], "tags")
                  newTagName.text = ""; newTagDesc.text = ""
                }
              }
            }
          }
        }
      }

      // ================= SETTINGS =================
      ColumnLayout {
        visible: m.tab === "settings"
        Layout.fillWidth: true
        spacing: Style.space(8)

        PanelSectionHeader { Layout.fillWidth: true; text: m.tr("set.section.scanning"); foreground: root.foreground; fontFamily: root.fontFamily }
        Segmented {
          Layout.fillWidth: true
          options: [{ value: "15m", label: "15m" }, { value: "30m", label: "30m" }, { value: "1h", label: "1h" }, { value: "6h", label: "6h" },
                    { value: "daily", label: m.tr("sched.daily") }, { value: "off", label: m.tr("sched.off") }]
          value: m.scheduleIndex
          onPicked: function(v) { m.run(["schedule", "set", v], "settings") }
        }
        Toggle {
          Layout.fillWidth: true
          label: m.tr("set.auto")
          description: m.autoTag ? m.tr("set.autoOn") : m.tr("set.autoOff")
          checked: m.autoTag
          foreground: root.foreground
          accent: root.accent
          fontFamily: root.fontFamily
          onClicked: m.run(["config", "set", "write_xattrs", m.autoTag ? "false" : "true"], "settings")
        }

        Text { Layout.fillWidth: true; horizontalAlignment: m.rtl ? Text.AlignRight : Text.AlignLeft; text: m.tr("set.folders"); color: root.foreground; font.family: root.fontFamily; font.pixelSize: root.body; font.bold: true }
        Repeater {
          model: m.folders
          delegate: RowLayout {
            required property var modelData
            Layout.fillWidth: true
            spacing: Style.space(2)
            Text {
              horizontalAlignment: m.rtl ? Text.AlignRight : Text.AlignLeft
              Layout.fillWidth: true
              text: modelData
              elide: Text.ElideMiddle
              color: root.foreground
              font.family: root.fontFamily
              font.pixelSize: root.body
            }
            Button {
              text: ""; iconText: root.iconClose
              fontFamily: root.fontFamily
              horizontalPadding: Style.space(9); verticalPadding: Style.space(6)
              foreground: root.foreground; accent: root.urgent; bordered: false
              tooltipText: m.tr("set.folderStop")
              Accessible.name: tooltipText
              onClicked: m.run(["dirs", "remove", modelData], "settings")
            }
          }
        }
        TextField {
          Layout.fillWidth: true
          placeholderText: m.tr("set.folderPh")
          font.family: root.fontFamily
          font.pixelSize: root.body
          foreground: root.foreground
          Accessible.name: m.tr("set.folderPh")
          onAccepted: { if (text.length > 0) { m.run(["dirs", "add", text], "settings"); text = "" } }
        }

        Text { Layout.fillWidth: true; horizontalAlignment: m.rtl ? Text.AlignRight : Text.AlignLeft; text: m.tr("set.ignored"); color: root.foreground; font.family: root.fontFamily; font.pixelSize: root.body; font.bold: true }
        Flow {
          Layout.fillWidth: true
          spacing: Style.space(5)
          layoutDirection: m.rtl ? Qt.RightToLeft : Qt.LeftToRight
          Repeater {
            model: m.ignores
            delegate: Row {
              required property var modelData
              spacing: Style.space(2)
              Chip { clickable: false; label: modelData }
              Chip { label: "×"; hint: m.tr("set.unignore", {name: modelData}); onClicked: m.run(["dirs", "unignore", modelData], "settings") }
            }
          }
        }
        TextField {
          Layout.fillWidth: true
          placeholderText: m.tr("set.ignorePh")
          font.family: root.fontFamily
          font.pixelSize: root.body
          foreground: root.foreground
          Accessible.name: m.tr("set.ignorePh")
          onAccepted: { if (text.length > 0) { m.run(["dirs", "ignore", text], "settings"); text = "" } }
        }
        Hint { text: m.tr("set.neverScanned") }

        PanelSectionHeader { Layout.fillWidth: true; text: m.tr("set.classifier"); foreground: root.foreground; fontFamily: root.fontFamily }
        Hint { visible: m.backends.length === 0; text: m.tr("set.noClassifier") }
        Repeater {
          model: m.backends
          delegate: Rectangle {
            id: bkCard
            required property var modelData
            readonly property bool open: root.openDetails[bkCard.modelData.name] === true
            Layout.fillWidth: true
            implicitHeight: bkColumn.implicitHeight + Style.space(16)
            radius: Style.space(5)
            color: Util.alpha(root.foreground, 0.04)
            border.color: bkCard.modelData.local ? Util.alpha(root.foreground, 0.14) : root.accentIcon
            border.width: 1
            ColumnLayout {
              id: bkColumn
              anchors.fill: parent
              anchors.margins: Style.space(8)
              spacing: Style.space(4)
              RowLayout {
                Layout.fillWidth: true
                spacing: Style.space(6)
                Text {
                  horizontalAlignment: m.rtl ? Text.AlignRight : Text.AlignLeft
                  Layout.fillWidth: true
                  text: bkCard.modelData.name + "  ·  " + (bkCard.modelData.model || bkCard.modelData.type)
                  elide: Text.ElideRight
                  color: root.foreground
                  font.family: root.fontFamily
                  font.pixelSize: root.body
                  font.bold: true
                }
                Chip {
                  visible: !bkCard.modelData.local && bkCard.modelData.name !== "proposer"
                  label: m.tr("set.apiKey"); hint: m.tr("set.apiKeyTip")
                  onClicked: m.openTerminal("secret set " + bkCard.modelData.name)
                }
              }
              Text {
                horizontalAlignment: m.rtl ? Text.AlignRight : Text.AlignLeft
                Layout.fillWidth: true
                wrapMode: Text.WordWrap
                text: bkCard.modelData.local ? m.tr("set.localShort", {host: bkCard.modelData.host}) : m.tr("set.remoteShort", {host: bkCard.modelData.host})
                color: bkCard.modelData.local ? root.dim : root.foreground
                font.family: root.fontFamily
                font.pixelSize: root.small
                font.bold: !bkCard.modelData.local
              }
              Text {
                horizontalAlignment: m.rtl ? Text.AlignRight : Text.AlignLeft
                Layout.fillWidth: true
                text: (bkCard.open ? "▾  " : "▸  ") + m.tr("set.details")
                color: root.foreground
                font.family: root.fontFamily
                font.pixelSize: root.small
                Accessible.role: Accessible.Button
                Accessible.name: text
                MouseArea {
                  anchors.fill: parent
                  cursorShape: Qt.PointingHandCursor
                  onClicked: { var o = root.openDetails; o[bkCard.modelData.name] = !bkCard.open; root.openDetails = Object.assign({}, o) }
                }
              }
              Hint { visible: bkCard.open; text: m.tr("role." + bkCard.modelData.role_code) + ". " + m.tr("mode." + bkCard.modelData.mode_code) + "." }
              Hint {
                visible: bkCard.open
                text: bkCard.modelData.local ? m.tr("set.runsLocal", {host: bkCard.modelData.host})
                                             : m.tr("set.runsRemote", {host: bkCard.modelData.host, key: m.tr("key." + bkCard.modelData.key_code)})
              }
            }
          }
        }
        Text {
          horizontalAlignment: m.rtl ? Text.AlignRight : Text.AlignLeft
          visible: m.usage.week !== undefined
          Layout.fillWidth: true
          text: (root.usageOpen ? "▾  " : "▸  ") + m.tr("set.usage") + "  ·  " + m.fmtTokens(m.usage.week)
          color: root.foreground
          font.family: root.fontFamily
          font.pixelSize: root.small
          Accessible.role: Accessible.Button
          Accessible.name: text
          MouseArea { anchors.fill: parent; cursorShape: Qt.PointingHandCursor; onClicked: root.usageOpen = !root.usageOpen }
        }
        Hint {
          visible: m.usage.week !== undefined && root.usageOpen
          text: m.tr("usage.line", {today: m.fmtTokens(m.usage.today), week: m.fmtTokens(m.usage.week)})
                + (m.usage.estimated ? " " + m.tr("usage.estimated") : "")
                + (m.usage.cost_week !== null && m.usage.cost_week !== undefined ? ", " + m.tr("usage.cost", {cost: m.usage.cost_week.toFixed(2)}) : "")
                + (m.usage.remaining ? "\n" + m.tr("usage.remaining", {tokens: m.fmtTokens(m.usage.remaining)})
                     + (m.usage.remaining_cost ? " " + m.tr("usage.remainingCost", {cost: m.usage.remaining_cost.toFixed(2)}) : "") : "")
        }
        Chip { label: m.tr("set.setup"); hint: m.tr("set.setupTip"); onClicked: m.openTerminal("setup") }

        PanelSectionHeader { Layout.fillWidth: true; text: m.tr("set.section.general"); foreground: root.foreground; fontFamily: root.fontFamily }
        Dropdown {
          Layout.fillWidth: true
          label: m.tr("set.language")
          value: m.languageSetting
          options: [{ value: "auto", label: m.tr("lang.auto") }, { value: "en", label: "English" }, { value: "nl", label: "Nederlands" },
                    { value: "fr", label: "Français" }, { value: "de", label: "Deutsch" }, { value: "es", label: "Español" },
                    { value: "zh", label: "中文" }, { value: "ar", label: "العربية" }]
          foreground: root.foreground
          accent: root.accent
          fontFamily: root.fontFamily
          onChanged: function(v) { m.setLanguage(v) }
        }
        Toggle {
          Layout.fillWidth: true
          label: m.tr("set.demo")
          description: m.tr("set.demoDesc")
          checked: m.demo
          foreground: root.foreground
          accent: root.accent
          fontFamily: root.fontFamily
          onClicked: m.setDemo(!m.demo)
        }
      }
    }
  }

  // ---- feedback ------------------------------------------------------------------------------------------------------------------
  RowLayout {
    visible: m.lastMessage !== "" || m.loadError !== ""
    Layout.fillWidth: true
    spacing: Style.space(6)
    Text {
      horizontalAlignment: m.rtl ? Text.AlignRight : Text.AlignLeft
      Layout.fillWidth: true
      wrapMode: Text.WordWrap
      text: m.lastMessage !== "" ? m.lastMessage : m.loadError
      color: root.foreground
      font.family: root.fontFamily
      font.pixelSize: root.small
    }
    Chip { visible: m.canUndo; label: m.tr("undo.button"); hint: m.tr("undo.tip"); accented: true; onClicked: m.run(["untag", "--path", m.undoPath]) }
  }
}
