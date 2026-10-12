// SONU's HUD (D80; redrawn in D93, D97): a separate Quickshell instance that sonud starts (`qs -n -p <this folder>`) — the
// owner's own shell and dotfiles are never touched. It shows what SONU is doing: listening, thinking,
// the steps as they happen (done / unverified / failed), the result, and — when SONU asks — the question
// with buttons. It reads sonud's state file and writes the answer file, both in SONU's 0700 runtime
// folder ($SONU_HUD_DIR); colours, corners and font come from the rice's colors.json, live. The look (D97,
// the owner: "simple but cooler"): an island — a slim capsule with one line, a glow that sweeps round its edge
// while SONU listens, thinks or works, voice bars that bounce and wave (a ✓ when done); it grows only for an
// answer to read or a question to answer.
import QtQuick
import QtQuick.Layouts
import QtQuick.Shapes
import Quickshell
import Quickshell.Io
import Quickshell.Wayland

ShellRoot {
    id: root

    readonly property string dir: Quickshell.env("SONU_HUD_DIR")
    property var hud: ({ state: "idle", seq: 0 })
    property int dismissed: -1          // the seq the user closed, or that timed out after "done"
    // up while SONU talks, even after "done" (found on the laptop: it hid mid-sentence)
    readonly property bool speaking: hud.speaking === true
    readonly property bool shown: hud.state !== "idle" && (hud.seq !== dismissed || speaking)
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
        running: root.hud.state === "done" && root.shown && !root.speaking  // counts from the end of speech
        onTriggered: root.dismissed = root.hud.seq
    }

    readonly property color accent: root.c.accentBright

    // ------------------------------------------------------------------ the island (D97)
    readonly property string mode: root.speaking ? "speaking" : (root.hud.state || "idle")
    readonly property int stepsDone: (root.hud.steps || []).filter(s => s.status !== "running").length
    readonly property int stepsAll: (root.hud.steps || []).length
    readonly property bool busy: root.mode === "thinking" || root.mode === "acting"
    readonly property bool open: root.asking || ((root.hud.result || "") !== "" && root.hud.state === "done")
                                 || (root.hud.details || "") !== ""
    readonly property var current: {
        const steps = root.hud.steps || []
        const running = steps.filter(s => s.status === "running")
        return running.length ? running[running.length - 1] : (steps.length ? steps[steps.length - 1] : null)
    }
    readonly property string headline: {
        if (root.mode === "listening") return "Listening…"
        if (root.asking) return root.hud.question.text
        if (root.current && root.busy) return root.current.text
        if (root.mode === "thinking") return (root.hud.request || "") !== "" ? "“" + root.hud.request + "”" : "Thinking…"
        return (root.hud.request || "") !== "" ? "“" + root.hud.request + "”" : "SONU"
    }

    PanelWindow {
        id: win
        visible: root.shown
        anchors.top: true            // top centre, under the bar (its exclusive zone is respected)
        margins.top: 8
        exclusionMode: ExclusionMode.Normal
        exclusiveZone: 0             // never pushes windows away
        implicitWidth: 560
        implicitHeight: pill.height + 16
        color: "transparent"
        WlrLayershell.layer: WlrLayer.Overlay
        WlrLayershell.namespace: "sonu-hud"
        WlrLayershell.keyboardFocus: root.asking ? WlrKeyboardFocus.OnDemand : WlrKeyboardFocus.None

        Item {
            id: pill
            anchors.horizontalCenter: parent.horizontalCenter
            y: 2
            width: root.open ? 540 : 380
            height: root.open ? Math.max(48, body.implicitHeight + 26) : 48
            Behavior on width { NumberAnimation { duration: 320; easing.type: Easing.OutBack; easing.overshoot: 0.6 } }
            Behavior on height { NumberAnimation { duration: 320; easing.type: Easing.OutCubic } }
            readonly property real r: Math.min(24, height / 2)

            // the living border: a glow that sweeps round while SONU thinks or works
            Shape {
                id: edge
                anchors.fill: parent
                anchors.margins: -1.5
                preferredRendererType: Shape.CurveRenderer
                property real turn: 0
                NumberAnimation on turn {
                    running: win.visible && (root.busy || root.mode === "listening" || root.asking)
                    loops: Animation.Infinite; from: 0; to: 360
                    duration: root.mode === "listening" ? 5200 : root.asking ? 3600 : 1700
                }
                ShapePath {
                    strokeWidth: 0; strokeColor: "transparent"
                    fillGradient: ConicalGradient {
                        centerX: edge.width / 2; centerY: edge.height / 2; angle: edge.turn
                        GradientStop { position: 0.0; color: root.accent }
                        GradientStop { position: 0.18; color: root.rgba(root.c.accentMid, 0.15) }
                        GradientStop { position: 0.5; color: root.rgba(root.c.accentMid, 0.08) }
                        GradientStop { position: 0.82; color: root.rgba(root.c.accentMid, 0.15) }
                        GradientStop { position: 1.0; color: root.accent }
                    }
                    PathRectangle { x: 0; y: 0; width: edge.width; height: edge.height; radius: pill.r + 1.5 }
                }
                opacity: root.mode === "done" && !root.speaking ? 0.35 : 1
                Behavior on opacity { NumberAnimation { duration: 400 } }
            }
            // the glass
            Rectangle {
                anchors.fill: parent
                radius: pill.r
                color: Qt.rgba(Qt.color(root.c.bg0).r, Qt.color(root.c.bg0).g, Qt.color(root.c.bg0).b, 1)  // solid
            }

            // left: the voice — bars that bounce while listening, wave while speaking, a check when done
            Item {
                id: voiceMark
                width: 26; height: 22
                anchors { left: parent.left; leftMargin: 18; top: parent.top; topMargin: 13 }
                Row {
                    anchors.centerIn: parent
                    spacing: 3
                    visible: root.mode !== "done" || root.speaking
                    Repeater {
                        model: 4
                        delegate: Rectangle {
                            id: bar
                            required property int index
                            width: 3; radius: 1.5
                            anchors.verticalCenter: parent.verticalCenter
                            color: root.accent
                            height: 5
                            SequentialAnimation on height {
                                running: win.visible && (root.mode === "listening" || root.mode === "speaking" || root.busy)
                                loops: Animation.Infinite
                                PauseAnimation { duration: bar.index * (root.mode === "speaking" ? 110 : 70) }
                                NumberAnimation {
                                    to: root.mode === "listening" ? 8 + (bar.index % 2) * 10 : root.mode === "speaking" ? 18 : 9
                                    duration: root.mode === "speaking" ? 260 : root.busy ? 420 : 300; easing.type: Easing.InOutSine
                                }
                                NumberAnimation { to: 4; duration: root.mode === "speaking" ? 260 : root.busy ? 420 : 300; easing.type: Easing.InOutSine }
                            }
                            opacity: root.busy ? 0.6 : 1
                        }
                    }
                }
                Text {
                    anchors.centerIn: parent
                    visible: root.mode === "done" && !root.speaking
                    text: "✓"
                    color: root.accent; font.family: root.font; font.pixelSize: 17; font.bold: true
                    scale: visible ? 1 : 0.4
                    Behavior on scale { NumberAnimation { duration: 260; easing.type: Easing.OutBack } }
                }
            }

            // right: the steps as dots, and close
            Row {
                id: right
                anchors { right: parent.right; rightMargin: 16; top: parent.top; topMargin: 19 }
                spacing: 5
                Repeater {
                    model: Math.min(root.stepsAll, 6)
                    delegate: Rectangle {
                        required property int index
                        width: 6; height: 6; radius: 3
                        readonly property var step: (root.hud.steps || [])[index]
                        color: step && step.status === "running" ? "transparent"
                             : step && step.status === "failed" ? root.c.critical : root.accent
                        border.width: 1; border.color: root.accent
                        opacity: step && step.status === "running" ? 0.9 : 0.8
                    }
                }
                Text {
                    text: "✕"; color: root.c.accentDim; font.family: root.font; font.pixelSize: 11
                    visible: !root.asking && root.mode === "done"
                    MouseArea { anchors.fill: parent; anchors.margins: -8; onClicked: root.dismissed = root.hud.seq }
                }
            }

            ColumnLayout {
                id: body
                anchors { left: voiceMark.right; right: right.left; top: parent.top; leftMargin: 12; rightMargin: 12; topMargin: 14 }
                spacing: 8

                // one line: what SONU is doing (or what you said)
                Text {
                    Layout.fillWidth: true
                    text: root.headline
                    color: root.asking ? root.accent : root.c.fg
                    font.family: root.font; font.pixelSize: 14; font.bold: root.asking
                    elide: Text.ElideRight
                    wrapMode: root.asking ? Text.Wrap : Text.NoWrap
                    maximumLineCount: root.asking ? 3 : 1
                }
                // the answer
                Text {
                    Layout.fillWidth: true
                    visible: (root.hud.result || "") !== "" && root.hud.state === "done"  // also while it's said
                    text: root.hud.result || ""
                    color: root.accent; font.family: root.font; font.pixelSize: 13
                    wrapMode: Text.Wrap; maximumLineCount: 5; elide: Text.ElideRight
                    opacity: visible ? 1 : 0
                    Behavior on opacity { NumberAnimation { duration: 300 } }
                }
                // something to read before answering (a prompt SONU wrote)
                Text {
                    Layout.fillWidth: true
                    visible: (root.hud.details || "") !== ""
                    text: root.hud.details || ""
                    color: root.c.fg; font.family: root.font; font.pixelSize: 12
                    wrapMode: Text.Wrap; maximumLineCount: 12; elide: Text.ElideRight
                }
                Text {
                    Layout.fillWidth: true
                    visible: root.asking && (root.hud.question.note || "") !== ""
                    text: root.asking ? (root.hud.question.note || "") : ""
                    color: root.c.accentMid; font.family: root.font; font.pixelSize: 12; wrapMode: Text.Wrap
                }
                // the answers
                Flow {
                    Layout.fillWidth: true
                    Layout.bottomMargin: 2
                    visible: root.asking && (root.hud.question.options || []).length > 0
                    spacing: 8
                    Repeater {
                        model: root.asking ? (root.hud.question.options || []) : []
                        delegate: Rectangle {
                            required property var modelData
                            required property int index
                            implicitWidth: label.implicitWidth + 30
                            implicitHeight: label.implicitHeight + 12
                            radius: height / 2
                            color: index === 0 ? (hover.containsMouse ? root.accent : root.rgba(root.c.accentBright, 0.85))
                                               : (hover.containsMouse ? root.rgba(root.c.accentMid, 0.25) : "transparent")
                            border.width: 1
                            border.color: index === 0 ? root.accent : root.rgba(root.c.accentMid, 0.6)
                            Text {
                                id: label
                                anchors.centerIn: parent
                                text: modelData.label
                                color: index === 0 ? root.c.bg0 : root.c.fg
                                font.family: root.font; font.pixelSize: 13; font.bold: index === 0
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
            }
        }
    }
}
