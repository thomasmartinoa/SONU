// SONU's HUD (D80, redrawn in D93): a separate Quickshell instance that sonud starts (`qs -n -p <this folder>`) — the
// owner's own shell and dotfiles are never touched. It shows what SONU is doing: listening, thinking,
// the steps as they happen (done / unverified / failed), the result, and — when SONU asks — the question
// with buttons. It reads sonud's state file and writes the answer file, both in SONU's 0700 runtime
// folder ($SONU_HUD_DIR); colours, corners and font come from the rice's colors.json, live. The look: a ring
// core that shows the state (breathing: listening, spinning: thinking, filling: the steps, ripples: speaking,
// ✓: done, a slow pulse: a question) beside a bracketed panel — thin lines, one accent, monospace.
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

    // ring speed and size by state: the core shows what SONU is doing at a glance
    readonly property string mode: root.speaking ? "speaking" : (root.hud.state || "idle")
    readonly property int stepsDone: (root.hud.steps || []).filter(s => s.status !== "running").length
    readonly property int stepsAll: (root.hud.steps || []).length
    readonly property color accent: root.c.accentBright
    readonly property color line: root.rgba(root.c.accentMid, 0.55)

    PanelWindow {
        id: win
        visible: root.shown
        anchors.top: true            // top centre, under the bar (its exclusive zone is respected)
        margins.top: 10
        exclusionMode: ExclusionMode.Normal
        exclusiveZone: 0             // never pushes windows away
        implicitWidth: 620
        implicitHeight: Math.max(132, body.implicitHeight + 34)
        color: "transparent"
        WlrLayershell.layer: WlrLayer.Overlay
        WlrLayershell.namespace: "sonu-hud"
        WlrLayershell.keyboardFocus: root.asking ? WlrKeyboardFocus.OnDemand : WlrKeyboardFocus.None

        // ---------------------------------------------------------------- the core: rings that show the state
        Item {
            id: core
            width: 112; height: 112
            anchors { left: parent.left; top: parent.top; leftMargin: 6; topMargin: 10 }

            // a soft disc behind the rings
            Rectangle {
                anchors.centerIn: parent
                width: 104; height: 104; radius: 52
                color: root.rgba(root.c.bg0, 0.96)
                border.width: 1
                border.color: root.rgba(root.c.accentMid, 0.25)
            }
            // outer ring: tick marks, turning slowly (fast while thinking)
            Shape {
                id: ticks
                anchors.fill: parent
                preferredRendererType: Shape.CurveRenderer
                ShapePath {
                    strokeColor: root.line; strokeWidth: 1; fillColor: "transparent"
                    strokeStyle: ShapePath.DashLine; dashPattern: [1, 5]
                    PathAngleArc { centerX: 56; centerY: 56; radiusX: 51; radiusY: 51; startAngle: 0; sweepAngle: 360 }
                }
                RotationAnimation on rotation {
                    running: win.visible; loops: Animation.Infinite; from: 0; to: 360
                    duration: root.mode === "thinking" ? 2400 : 18000
                }
            }
            // two arcs turning against each other
            Shape {
                anchors.fill: parent
                preferredRendererType: Shape.CurveRenderer
                ShapePath {
                    strokeColor: root.accent; strokeWidth: 2; fillColor: "transparent"; capStyle: ShapePath.RoundCap
                    PathAngleArc { centerX: 56; centerY: 56; radiusX: 43; radiusY: 43; startAngle: -90; sweepAngle: 70 }
                }
                ShapePath {
                    strokeColor: root.accent; strokeWidth: 2; fillColor: "transparent"; capStyle: ShapePath.RoundCap
                    PathAngleArc { centerX: 56; centerY: 56; radiusX: 43; radiusY: 43; startAngle: 90; sweepAngle: 70 }
                }
                RotationAnimation on rotation {
                    running: win.visible && root.mode !== "done"; loops: Animation.Infinite; from: 0; to: 360
                    duration: root.mode === "thinking" ? 1200 : root.mode === "acting" ? 2000 : 5200
                }
            }
            Shape {
                anchors.fill: parent
                preferredRendererType: Shape.CurveRenderer
                opacity: 0.7
                ShapePath {
                    strokeColor: root.c.accentMid; strokeWidth: 1; fillColor: "transparent"
                    PathAngleArc { centerX: 56; centerY: 56; radiusX: 36; radiusY: 36; startAngle: 0; sweepAngle: 250 }
                }
                RotationAnimation on rotation {
                    running: win.visible && root.mode !== "done"; loops: Animation.Infinite; from: 360; to: 0
                    duration: root.mode === "thinking" ? 1800 : 7000
                }
            }
            // the steps: an arc that fills as they finish
            Shape {
                anchors.fill: parent
                visible: root.stepsAll > 0
                preferredRendererType: Shape.CurveRenderer
                ShapePath {
                    strokeColor: root.rgba(root.c.accentMid, 0.25); strokeWidth: 3; fillColor: "transparent"
                    PathAngleArc { centerX: 56; centerY: 56; radiusX: 29; radiusY: 29; startAngle: -90; sweepAngle: 360 }
                }
                ShapePath {
                    strokeColor: root.accent; strokeWidth: 3; fillColor: "transparent"; capStyle: ShapePath.RoundCap
                    PathAngleArc {
                        centerX: 56; centerY: 56; radiusX: 29; radiusY: 29; startAngle: -90
                        sweepAngle: root.stepsAll ? 360 * root.stepsDone / root.stepsAll : 0
                        Behavior on sweepAngle { NumberAnimation { duration: 400; easing.type: Easing.OutCubic } }
                    }
                }
            }
            // the centre: breathes while listening, ripples while speaking, a mark when done or asking
            Rectangle {
                id: heart
                anchors.centerIn: parent
                width: 22; height: 22; radius: 11
                color: root.mode === "listening" || root.mode === "speaking" || root.mode === "asking" ? root.accent : root.c.accentMid
                SequentialAnimation on scale {
                    running: win.visible && (root.mode === "listening" || root.mode === "asking")
                    loops: Animation.Infinite
                    NumberAnimation { to: root.mode === "asking" ? 1.25 : 1.45; duration: root.mode === "asking" ? 900 : 650; easing.type: Easing.InOutSine }
                    NumberAnimation { to: 1.0; duration: root.mode === "asking" ? 900 : 650; easing.type: Easing.InOutSine }
                }
                Text {
                    anchors.centerIn: parent
                    visible: root.mode === "done" || root.mode === "asking"
                    text: root.mode === "asking" ? "?" : "✓"
                    color: root.c.bg0; font.family: root.font; font.pixelSize: 13; font.bold: true
                }
            }
            // speaking: rings spreading from the centre
            Repeater {
                model: 2
                delegate: Rectangle {
                    id: ripple
                    required property int index
                    anchors.centerIn: parent
                    width: 22; height: 22; radius: width / 2
                    color: "transparent"; border.width: 1.5; border.color: root.accent
                    visible: root.mode === "speaking"
                    opacity: 0
                    ParallelAnimation {
                        running: root.mode === "speaking" && win.visible
                        loops: Animation.Infinite
                        PauseAnimation { duration: index * 600 }
                        NumberAnimation { target: ripple; property: "width"; from: 22; to: 84; duration: 1200; easing.type: Easing.OutCubic }
                        NumberAnimation { target: ripple; property: "height"; from: 22; to: 84; duration: 1200; easing.type: Easing.OutCubic }
                        NumberAnimation { target: ripple; property: "opacity"; from: 0.9; to: 0; duration: 1200 }
                    }
                }
            }
        }

        // ---------------------------------------------------------------- the panel: brackets, not a box
        Item {
            id: panel
            anchors { left: core.right; right: parent.right; top: parent.top; leftMargin: 10; topMargin: 6 }
            height: body.implicitHeight + 22

            Rectangle {  // the glass behind the text
                anchors.fill: parent
                color: root.rgba(root.c.bg0, 0.96)
                radius: root.radius
            }
            // corner brackets
            Repeater {
                model: 4
                delegate: Item {
                    required property int index
                    width: 14; height: 14
                    x: index % 2 === 0 ? 0 : panel.width - width
                    y: index < 2 ? 0 : panel.height - height
                    Rectangle { width: parent.width; height: 1.5; color: root.accent; y: index < 2 ? 0 : parent.height - 1.5 }
                    Rectangle { width: 1.5; height: parent.height; color: root.accent; x: index % 2 === 0 ? 0 : parent.width - 1.5 }
                }
            }
            // a hairline along the top and bottom between the brackets
            Rectangle { x: 18; width: panel.width - 36; height: 1; color: root.line }
            Rectangle { x: 18; y: panel.height - 1; width: panel.width - 36; height: 1; color: root.line }

            ColumnLayout {
                id: body
                anchors { left: parent.left; right: parent.right; top: parent.top; margins: 14; topMargin: 11 }
                spacing: 7

                // SONU · STATE ········· ✕
                RowLayout {
                    spacing: 8
                    Text {
                        text: "SONU"
                        color: root.accent; font.family: root.font; font.pixelSize: 11; font.bold: true
                        font.letterSpacing: 2.5
                    }
                    Text {
                        text: (root.mode === "speaking" ? "speaking" : root.stateWord(root.hud.state)).toUpperCase()
                        color: root.c.accentMid; font.family: root.font; font.pixelSize: 11; font.letterSpacing: 2
                    }
                    Rectangle { Layout.fillWidth: true; height: 1; color: root.line; Layout.alignment: Qt.AlignVCenter }
                    Text {
                        visible: root.stepsAll > 0
                        text: root.stepsDone + "/" + root.stepsAll
                        color: root.c.accentMid; font.family: root.font; font.pixelSize: 11
                    }
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

                // the steps, as a timeline
                Repeater {
                    model: root.hud.steps || []
                    delegate: RowLayout {
                        required property var modelData
                        required property int index
                        Layout.fillWidth: true
                        spacing: 9
                        visible: !(root.asking && modelData.status === "running")  // the question says it
                        Item {
                            Layout.preferredWidth: 12; Layout.preferredHeight: 18
                            Layout.alignment: Qt.AlignTop
                            Rectangle {  // the node
                                anchors.centerIn: parent
                                width: 7; height: 7; radius: 3.5
                                color: modelData.status === "running" ? "transparent" : root.statusColour(modelData.status)
                                border.width: 1.5
                                border.color: modelData.status === "running" ? root.accent : root.statusColour(modelData.status)
                                SequentialAnimation on opacity {
                                    running: modelData.status === "running"; loops: Animation.Infinite
                                    NumberAnimation { to: 0.2; duration: 500 }
                                    NumberAnimation { to: 1; duration: 500 }
                                }
                            }
                        }
                        Text {
                            Layout.fillWidth: true
                            text: (modelData.status === "failed" ? "✖ " : modelData.status === "unverified" ? "! " : "") + modelData.text
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
                    radius: root.radius; color: root.rgba(root.c.bg2, 0.9)
                    border.width: 1; border.color: root.line
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
                    color: root.accent
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
                            implicitWidth: label.implicitWidth + 30
                            implicitHeight: label.implicitHeight + 14
                            radius: 2
                            color: hover.containsMouse ? root.rgba(root.c.accentMid, 0.18) : "transparent"
                            border.width: 1
                            border.color: index === 0 ? root.accent : root.line
                            Text {
                                id: label
                                anchors.centerIn: parent
                                text: "[ " + modelData.label + " ]"
                                color: index === 0 ? root.accent : root.c.fg
                                font.family: root.font; font.pixelSize: 13
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
                    color: root.accent; font.family: root.font; font.pixelSize: 13
                    wrapMode: Text.Wrap; maximumLineCount: 5; elide: Text.ElideRight
                }
            }
        }
    }
}
