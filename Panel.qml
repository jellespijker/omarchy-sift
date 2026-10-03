import QtQuick
import QtQuick.Layouts
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui

// Sift: the bar icon and its popup. Data lives in Model.qml, the view in Content.qml; this file only hosts them in the shell.
Panel {
  id: root
  moduleName: "jellespijker.sift"
  ipcTarget: ""
  manageIpc: false

  Model { id: model; opened: root.opened }

  IpcHandler {
    target: "jellespijker.sift"
    function open(): void { root.open() }
    function close(): void { root.close() }
    function toggle(): void { root.toggle() }
    function refresh(): void { model.refresh() }
    function scan(): void { model.scan() }
    function tab(name: string): void { model.tab = name; root.open() }
    function demo(on: bool): void { model.setDemo(on) }
  }

  readonly property color urgent: bar ? bar.urgent : Color.urgent
  readonly property color accent: Color.accent
  readonly property int barContentWidth: Style.bar.iconFont + Style.space(4)
  readonly property int barSlot: barContentWidth + Style.space(10)
  implicitWidth: bar && bar.vertical ? (bar ? bar.barSize : Style.bar.sizeHorizontal) : barSlot
  implicitHeight: bar && bar.vertical ? barSlot : (bar ? bar.barSize : Style.bar.sizeHorizontal)

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: model.isError ? "󰀨" : (model.attention > 0 ? "󰓺" : "󰓹")
    slotSize: root.barSlot
    opticalSize: root.barContentWidth
    active: model.isError || model.attention > 0
    activeColor: model.isError ? root.urgent : root.accent
    useActiveColor: true
    tooltipText: model.isError
      ? model.tr("tip.error", {state: model.backendState, detail: model.backendError ? " (" + model.backendError + ")" : ""})
      : (model.attention > 0
         ? (model.ideaCount > 0 ? model.tr("tip.attentionIdeas", {n: model.pending, k: model.ideaCount}) : model.tr("tip.attention", {n: model.pending}))
         : model.tr("tip.quiet"))
    onPressed: function(b) {
      if (b === Qt.RightButton || b === Qt.MiddleButton) model.refresh()
      else root.toggle()
    }
  }

  KeyboardPanel {
    id: panel
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.opened
    focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(480))
    contentHeight: panel.fittedContentHeight(content.implicitHeight, Style.space(620))

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      onCloseRequested: { if (model.confirm) model.confirmNo(); else root.close() }
      onTabRequested: function(direction) { root.switchPanel(direction) }
      onMoveRequested: function(dx, dy) { content.moveSelection(dy) }
      onActivateRequested: content.acceptSelected()
      onTextKey: function(t) {
        if (t === "r" || t === "R") model.refresh()
        if (t === "s" || t === "S") model.scan()
        if (t === "x" || t === "X") content.rejectSelected()
      }

      Content {
        id: content
        anchors.fill: parent
        m: model
        bar: root.bar
      }
    }
  }
}
