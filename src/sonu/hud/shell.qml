// SONU's HUD (D80): a separate Quickshell instance that sonud starts (`qs -n -p <this folder>`) — the
// owner's own shell and dotfiles are never touched. It shows what SONU is doing: listening, thinking,
// the steps as they happen (done / unverified / failed), the result, and — when SONU asks — the question
// with buttons. It reads sonud's state file and writes the answer file, both in SONU's 0700 runtime
// folder ($SONU_HUD_DIR); colours, corners and font come from the rice's colors.json, live.
import QtQuick
import QtQuick.Layouts
import Quickshell
import Quickshell.Io
import Quickshell.Wayland

ShellRoot {
    id: root

    readonly property string dir: Quickshell.env("SONU_HUD_DIR")
    property var hud: ({ state: "idle", seq: 0 })
    property int dismissed: -1          // the seq the user closed, or that timed out after "done"
    readonly property bool shown: hud.state !== "idle" && hud.seq !== dismissed
    readonly property bool asking: hud.state === "asking" && hud.question !== undefined && hud.question !== null
    readonly property int hideAfterMs: (hud.hide_after_s || 6) * 1000

    // the rice's palette (Orrery): the same file its own shell watches
    property var c: ({
        bg0: "#0a0a0a", bg1: "#141414", bg2: "#1e1e1e", bg3: "#282828",
        fg: "#e8e8e8", accentBright: "#ffffff", accentMid: "#a0a0a0", accentDim: "#606060",
        good: "#b0b0b0", warning: "#b0b0b0", critical: "#808080", border: "#3de0e0e0"
    })
    property int radius: 4
    property bool hued: false
    readonly property string font: "JetBrainsMono Nerd Font Propo"

    function rgba(col, a) { const q = Qt.color(col); return Qt.rgba(q.r, q.g, q.b, a) }
    function statusColour(s) {
        if (!root.hued) return s === "failed" ? root.c.accentDim : root.c.fg
        return s === "done" ? root.c.good : s === "failed" ? root.c.critical
             : s === "unverified" ? root.c.warning : root.c.fg
    }
    function icon(s) {
        return s === "done" ? "✔" : s === "failed" ? "✖" : s === "unverified" ? "!" : s === "skipped" ? "·"
             : s === "denied" ? "⊘" : s === "needs-input" ? "?" : "…"
    }
    function stateWord(s) {
        return s === "listening" ? "listening" : s === "thinking" ? "thinking" : s === "acting" ? "working"
             : s === "asking" ? "asking" : s === "done" ? "done" : ""
    }
    function answer(value) {
        if (!root.asking) return
        answerFile.setText(JSON.stringify({ id: root.hud.question.id, value: value }) + "\n")
        root.dismissed = -1
    }

    FileView {
        id: stateFile
        path: root.dir + "/state.json"
        watchChanges: true
        onFileChanged: reload()
        onLoaded: {
            try {
                const next = JSON.parse(text())
                if (next.seq !== root.hud.seq) root.hud = next
            } catch (e) { /* half-written: the next change or poll reads it */ }
        }
        onLoadFailed: (err) => {}
    }
    // sonud replaces the file atomically, which can outrun the watcher: read it again a few times a second
    Timer { interval: 200; running: true; repeat: true; onTriggered: stateFile.reload() }
    FileView { id: answerFile; path: root.dir + "/answer.json"; blockWrites: false; preload: false; printErrors: false }

    FileView {
        id: colours
        path: Quickshell.env("HOME") + "/.config/orrery/current/colors.json"
        watchChanges: true
        onFileChanged: reload()
        onLoaded: {
            try {
                const j = JSON.parse(text())
                if (j.colors) root.c = Object.assign({}, root.c, j.colors)
                if (j.radius !== undefined) root.radius = j.radius
                root.hued = j.hued === true
            } catch (e) {}
        }
        onLoadFailed: (err) => {}
    }

    // after "done", the HUD stays a few seconds, then goes
    Timer {
        id: hideTimer
        interval: root.hideAfterMs
        running: root.hud.state === "done" && root.shown
        onTriggered: root.dismissed = root.hud.seq
    }

    PanelWindow {
        id: win
        visible: root.shown
        anchors.top: true            // top centre, under the bar (its exclusive zone is respected)
        margins.top: 8
        exclusionMode: ExclusionMode.Normal
        exclusiveZone: 0             // never pushes windows away
        implicitWidth: 480
        implicitHeight: body.implicitHeight + 28
        color: "transparent"
        WlrLayershell.layer: WlrLayer.Overlay
        WlrLayershell.namespace: "sonu-hud"
        WlrLayershell.keyboardFocus: root.asking ? WlrKeyboardFocus.OnDemand : WlrKeyboardFocus.None

        Rectangle {
            anchors.fill: parent
            radius: root.radius
            color: root.c.bg0
            border.width: 1
            border.color: root.c.border

            ColumnLayout {
                id: body
                anchors { left: parent.left; right: parent.right; top: parent.top; margins: 14 }
                spacing: 8

                // SONU · state
                RowLayout {
                    spacing: 8
                    Rectangle {
                        id: dot
                        width: 9; height: 9; radius: 4.5
                        color: root.hud.state === "listening" ? root.c.accentBright : root.c.accentMid
                        SequentialAnimation on opacity {
                            running: ["listening", "thinking", "acting"].indexOf(root.hud.state) >= 0
                            loops: Animation.Infinite
                            NumberAnimation { to: 0.25; duration: 550 }
                            NumberAnimation { to: 1; duration: 550 }
                        }
                    }
                    Text {
                        text: "SONU · " + root.stateWord(root.hud.state)
                        color: root.c.accentMid; font.family: root.font; font.pixelSize: 12
                    }
                    Item { Layout.fillWidth: true }
                    Text {
                        text: "✕"; color: root.c.accentDim; font.family: root.font; font.pixelSize: 12
                        visible: !root.asking
                        MouseArea { anchors.fill: parent; anchors.margins: -6; onClicked: root.dismissed = root.hud.seq }
                    }
                }

                // what the user asked
                Text {
                    Layout.fillWidth: true
                    visible: (root.hud.request || "") !== ""
                    text: "“" + (root.hud.request || "") + "”"
                    color: root.c.fg; font.family: root.font; font.pixelSize: 14
                    wrapMode: Text.Wrap; maximumLineCount: 3; elide: Text.ElideRight
                }

                // the steps, as they happen
                Repeater {
                    model: root.hud.steps || []
                    delegate: RowLayout {
                        required property var modelData
                        Layout.fillWidth: true
                        spacing: 8
                        visible: !(root.asking && modelData.status === "running")  // the question says it
                        Text {
                            text: root.icon(modelData.status)
                            color: root.statusColour(modelData.status)
                            font.family: root.font; font.pixelSize: 13
                            Layout.alignment: Qt.AlignTop
                        }
                        Text {
                            Layout.fillWidth: true
                            text: modelData.text
                            color: modelData.status === "running" ? root.c.accentMid : root.c.fg
                            font.family: root.font; font.pixelSize: 13
                            wrapMode: Text.Wrap; maximumLineCount: 4; elide: Text.ElideRight
                        }
                    }
                }

                // something to read before answering (a prompt SONU wrote)
                Rectangle {
                    Layout.fillWidth: true
                    visible: (root.hud.details || "") !== ""
                    implicitHeight: detailsText.implicitHeight + 16
                    radius: root.radius; color: root.c.bg2
                    Text {
                        id: detailsText
                        anchors { left: parent.left; right: parent.right; top: parent.top; margins: 8 }
                        text: root.hud.details || ""
                        color: root.c.fg; font.family: root.font; font.pixelSize: 12
                        wrapMode: Text.Wrap; maximumLineCount: 14; elide: Text.ElideRight
                    }
                }

                // the question, and its answers
                Text {
                    Layout.fillWidth: true
                    visible: root.asking
                    text: root.asking ? root.hud.question.text : ""
                    color: root.hud.question && root.hud.question.risk === "high" ? root.c.accentBright : root.c.fg
                    font.family: root.font; font.pixelSize: 14; font.bold: true
                    wrapMode: Text.Wrap
                }
                Text {
                    Layout.fillWidth: true
                    visible: root.asking && (root.hud.question.note || "") !== ""
                    text: root.asking ? (root.hud.question.note || "") : ""
                    color: root.c.accentMid; font.family: root.font; font.pixelSize: 12; wrapMode: Text.Wrap
                }
                Flow {
                    Layout.fillWidth: true
                    visible: root.asking && (root.hud.question.options || []).length > 0
                    spacing: 8
                    Repeater {
                        model: root.asking ? (root.hud.question.options || []) : []
                        delegate: Rectangle {
                            required property var modelData
                            required property int index
                            implicitWidth: label.implicitWidth + 28
                            implicitHeight: label.implicitHeight + 14
                            radius: root.radius
                            color: hover.containsMouse ? root.c.bg3 : root.c.bg2
                            border.width: 1
                            border.color: index === 0 ? root.c.accentMid : root.c.border
                            Text {
                                id: label
                                anchors.centerIn: parent
                                text: modelData.label
                                color: root.c.fg; font.family: root.font; font.pixelSize: 13
                            }
                            MouseArea {
                                id: hover
                                anchors.fill: parent
                                hoverEnabled: true
                                cursorShape: Qt.PointingHandCursor
                                onClicked: root.answer(modelData.value)
                            }
                        }
                    }
                }

                // the result (or SONU's reply)
                Text {
                    Layout.fillWidth: true
                    visible: root.hud.state === "done" && (root.hud.result || "") !== ""
                    text: root.hud.result || ""
                    color: root.c.accentBright; font.family: root.font; font.pixelSize: 13
                    wrapMode: Text.Wrap; maximumLineCount: 4; elide: Text.ElideRight
                }
            }
        }
    }
}
