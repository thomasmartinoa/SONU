// SONU's Control Center (D90, part 2): its own Quickshell window (`sonu control` starts `qs -n -p <this>`),
// so the owner's shell and dotfiles are never touched. Every change goes through `sonu settings set` — the
// same checks as the terminal: only the catalogued settings, the whole file validated before it's written,
// a change that loosens privacy or safety asked here first, every change audited, sonud restarted once idle.
// Colours, corners and font come from the rice's colors.json, live.
import QtQuick
import QtQuick.Layouts
import Quickshell
import Quickshell.Io

ShellRoot {
    id: root

    readonly property string sonu: Quickshell.env("SONU_BIN") || "sonu"
    readonly property var groups: ["Brain", "Voice", "Proactive", "Privacy", "Look"]
    property string group: "Brain"
    property var rows: []
    property var models: []          // installed local models that can plan: { name, gb }
    property string status: ""
    property bool busy: false
    property var asking: null        // { row, value, arg } waiting for "are you sure?"

    // the rice's palette (Orrery): the same file its own shell watches
    property var c: ({
        bg0: "#0a0a0a", bg1: "#141414", bg2: "#1e1e1e", bg3: "#282828",
        fg: "#e8e8e8", accentBright: "#ffffff", accentMid: "#a0a0a0", accentDim: "#606060",
        border: "#3de0e0e0"
    })
    property int radius: 4
    readonly property string font: "JetBrainsMono Nerd Font Propo"

    function shown(v) {
        if (v === true) return "on"
        if (v === false) return "off"
        if (v === null || v === undefined) return "not set"
        if (typeof v === "object" && v.length !== undefined)  // a list (Qt's, not always a JS Array)
            return v.length ? Array.prototype.join.call(v, ", ") : "none"
        return String(v)
    }
    function arg(row, v) {
        if (row.kind === "bool") return v ? "on" : "off"
        if (row.kind === "list") return Array.prototype.join.call(v, ",")
        if (v === null || v === undefined) return ""
        return String(v)
    }
    function loosens(row, v) {
        if (row.kind === "list") return v.some(x => row.loosens.indexOf(x) >= 0 && row.value.indexOf(x) < 0)
        return v !== row.value && row.loosens.indexOf(v) >= 0
    }
    function change(row, v) {
        if (root.busy || JSON.stringify(v) === JSON.stringify(row.value)) return
        if (loosens(row, v)) { root.asking = { row: row, value: v }; return }
        set(row, v, false)
    }
    function set(row, v, confirmed) {
        const command = [root.sonu, "settings", "set", row.key, arg(row, v)]
        if (confirmed) command.push("--yes")  // only after the user said yes here
        root.busy = true
        root.status = "Changing " + row.label + "…"
        setter.command = command
        setter.running = true
    }
    function reload() { lister.running = true }

    Process {
        id: lister
        command: [root.sonu, "settings", "list", "--json"]
        running: true
        stdout: StdioCollector {
            onStreamFinished: {
                try { root.rows = JSON.parse(text) } catch (e) { root.status = "Couldn't read the settings" }
            }
        }
        stderr: StdioCollector { onStreamFinished: if (text.trim()) root.status = text.trim() }
    }
    Process {
        id: modelLister
        command: [root.sonu, "settings", "models", "--json"]
        running: true
        stdout: StdioCollector {
            onStreamFinished: {
                try {
                    root.models = JSON.parse(text)
                        .filter(m => m.tools && !m.cloud && m.name.indexOf("embed") < 0)
                        .sort((a, b) => a.gb - b.gb)
                } catch (e) {}
            }
        }
    }
    Process {
        id: setter
        stdout: StdioCollector { id: setOut }
        stderr: StdioCollector { id: setErr }
        onExited: (code, status) => {
            const said = (code === 0 ? setOut.text : setErr.text || setOut.text).trim().split("\n")
            root.status = said[said.length - 1].replace(/\[[^\]]*\]/g, "")
            root.busy = false
            root.reload()
        }
    }

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
            } catch (e) {}
        }
        onLoadFailed: (err) => {}
    }

    // ------------------------------------------------------------------ small controls
    component Label: Text { color: root.c.fg; font.family: root.font; font.pixelSize: 13 }
    component Chip: Rectangle {
        id: chip
        property string text
        property bool on: false
        signal clicked()
        implicitWidth: chipText.implicitWidth + 22
        implicitHeight: chipText.implicitHeight + 10
        radius: root.radius
        color: on ? root.c.bg3 : (chipHover.containsMouse ? root.c.bg2 : root.c.bg1)
        border.width: 1
        border.color: on ? root.c.accentMid : root.c.border
        opacity: root.busy ? 0.6 : 1
        Text {
            id: chipText
            anchors.centerIn: parent
            text: chip.text
            color: chip.on ? root.c.accentBright : root.c.fg
            font.family: root.font; font.pixelSize: 12
        }
        MouseArea {
            id: chipHover
            anchors.fill: parent
            hoverEnabled: true
            cursorShape: Qt.PointingHandCursor
            onClicked: chip.clicked()
        }
    }

    FloatingWindow {
        id: win
        title: "SONU Control Center"
        implicitWidth: 820
        implicitHeight: 600
        // a fixed size: Hyprland floats it (a dialog), instead of tiling it into the workspace
        minimumSize: Qt.size(820, 600)
        maximumSize: Qt.size(820, 600)
        color: root.c.bg0
        onClosed: Qt.quit()

        RowLayout {
            anchors.fill: parent
            spacing: 0

            // the pages
            Rectangle {
                Layout.fillHeight: true
                Layout.preferredWidth: 170
                color: root.c.bg1
                ColumnLayout {
                    anchors { left: parent.left; right: parent.right; top: parent.top; margins: 14 }
                    spacing: 6
                    Text {
                        text: "SONU"
                        color: root.c.accentBright; font.family: root.font; font.pixelSize: 18; font.bold: true
                        Layout.bottomMargin: 10
                    }
                    Repeater {
                        model: root.groups
                        delegate: Rectangle {
                            required property string modelData
                            Layout.fillWidth: true
                            implicitHeight: 32
                            radius: root.radius
                            color: root.group === modelData ? root.c.bg3 : (pageHover.containsMouse ? root.c.bg2 : "transparent")
                            Text {
                                anchors { left: parent.left; leftMargin: 10; verticalCenter: parent.verticalCenter }
                                text: modelData
                                color: root.group === modelData ? root.c.accentBright : root.c.fg
                                font.family: root.font; font.pixelSize: 13
                            }
                            MouseArea {
                                id: pageHover
                                anchors.fill: parent
                                hoverEnabled: true
                                cursorShape: Qt.PointingHandCursor
                                onClicked: root.group = modelData
                            }
                        }
                    }
                }
            }

            // the settings of this page
            ColumnLayout {
                Layout.fillWidth: true
                Layout.fillHeight: true
                spacing: 0

                Flickable {
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    clip: true
                    contentHeight: list.implicitHeight + 28
                    ColumnLayout {
                        id: list
                        anchors { left: parent.left; right: parent.right; top: parent.top; margins: 18 }
                        spacing: 14
                        Repeater {
                            model: root.rows.filter(r => r.group === root.group)
                            delegate: ColumnLayout {
                                id: rowItem
                                required property var modelData
                                Layout.fillWidth: true
                                spacing: 6
                                RowLayout {
                                    Layout.fillWidth: true
                                    Label { text: rowItem.modelData.label; font.bold: true }
                                    Item { Layout.fillWidth: true }
                                    Text {
                                        text: root.shown(rowItem.modelData.value)
                                        color: root.c.accentMid; font.family: root.font; font.pixelSize: 12
                                    }
                                }
                                Text {
                                    Layout.fillWidth: true
                                    visible: rowItem.modelData.help !== ""
                                    text: rowItem.modelData.help
                                    color: root.c.accentDim; font.family: root.font; font.pixelSize: 11
                                    wrapMode: Text.Wrap
                                }
                                Loader {
                                    Layout.fillWidth: true
                                    property var row: rowItem.modelData
                                    sourceComponent: row.kind === "bool" ? boolEditor
                                        : row.kind === "choice" ? choiceEditor
                                        : row.kind === "model" ? modelEditor
                                        : row.kind === "list" ? listEditor
                                        : (row.kind === "int" || row.kind === "float") ? numberEditor
                                        : textEditor
                                }
                            }
                        }
                    }
                }

                // what happened
                Rectangle {
                    Layout.fillWidth: true
                    implicitHeight: 34
                    color: root.c.bg1
                    Text {
                        anchors { left: parent.left; right: parent.right; leftMargin: 14; rightMargin: 14; verticalCenter: parent.verticalCenter }
                        text: root.status || "Changes are checked, written to your config (comments kept) and logged; SONU restarts by itself to use them."
                        color: root.c.accentMid; font.family: root.font; font.pixelSize: 11
                        elide: Text.ElideRight
                    }
                }
            }
        }

        // "are you sure?" for a change that loosens privacy or safety
        Rectangle {
            anchors.fill: parent
            visible: root.asking !== null
            color: Qt.rgba(0, 0, 0, 0.6)
            MouseArea { anchors.fill: parent }  // nothing behind it is clickable meanwhile
            Rectangle {
                anchors.centerIn: parent
                width: 460
                height: sure.implicitHeight + 36
                radius: root.radius
                color: root.c.bg1
                border.width: 1
                border.color: root.c.accentMid
                ColumnLayout {
                    id: sure
                    anchors { left: parent.left; right: parent.right; top: parent.top; margins: 18 }
                    spacing: 10
                    Label {
                        Layout.fillWidth: true
                        text: root.asking ? root.asking.row.label + " → " + root.shown(root.asking.value) + "?" : ""
                        font.bold: true; font.pixelSize: 15; wrapMode: Text.Wrap
                    }
                    Text {
                        Layout.fillWidth: true
                        text: root.asking ? root.asking.row.help + " This gives SONU more reach or keeps more of your data." : ""
                        color: root.c.accentMid; font.family: root.font; font.pixelSize: 12; wrapMode: Text.Wrap
                    }
                    RowLayout {
                        spacing: 8
                        Chip { text: "Yes, change it"; on: true; onClicked: { const a = root.asking; root.asking = null; root.set(a.row, a.value, true) } }
                        Chip { text: "No"; onClicked: root.asking = null }
                    }
                }
            }
        }
    }

    // ------------------------------------------------------------------ one editor per kind
    Component {
        id: boolEditor
        Flow {
            id: toggle
            property var row: parent ? parent.row : null
            spacing: 6
            Chip { text: "On"; on: toggle.row !== null && toggle.row.value === true; onClicked: root.change(toggle.row, true) }
            Chip { text: "Off"; on: toggle.row !== null && toggle.row.value === false; onClicked: root.change(toggle.row, false) }
        }
    }
    Component {
        id: choiceEditor
        Flow {
            id: choices
            property var row: parent ? parent.row : null
            spacing: 6
            Repeater {
                model: choices.row ? choices.row.choices : []
                delegate: Chip {
                    required property string modelData
                    text: modelData
                    on: choices.row.value === modelData
                    onClicked: root.change(choices.row, modelData)
                }
            }
        }
    }
    Component {
        id: modelEditor
        ColumnLayout {
            id: pick
            property var row: parent ? parent.row : null
            property bool open: false
            spacing: 4
            Chip {
                text: (pick.row ? pick.row.value : "") + "   " + (pick.open ? "▴" : "▾")
                on: pick.open
                onClicked: pick.open = !pick.open
            }
            Repeater {
                model: pick.open ? root.models : []
                delegate: Rectangle {
                    required property var modelData
                    Layout.preferredWidth: 420
                    implicitHeight: 28
                    radius: root.radius
                    color: optionHover.containsMouse ? root.c.bg2 : root.c.bg1
                    border.width: pick.row.value === modelData.name ? 1 : 0
                    border.color: root.c.accentMid
                    Text {
                        anchors { left: parent.left; leftMargin: 10; verticalCenter: parent.verticalCenter }
                        text: modelData.name
                        color: root.c.fg; font.family: root.font; font.pixelSize: 12
                        elide: Text.ElideRight; width: 320
                    }
                    Text {
                        anchors { right: parent.right; rightMargin: 10; verticalCenter: parent.verticalCenter }
                        text: modelData.gb + " GB"
                        color: root.c.accentDim; font.family: root.font; font.pixelSize: 11
                    }
                    MouseArea {
                        id: optionHover
                        anchors.fill: parent
                        hoverEnabled: true
                        cursorShape: Qt.PointingHandCursor
                        onClicked: { pick.open = false; root.change(pick.row, modelData.name) }
                    }
                }
            }
        }
    }
    Component {
        id: listEditor
        Flow {
            id: items
            property var row: parent ? parent.row : null
            spacing: 6
            Repeater {
                model: items.row ? items.row.choices : []
                delegate: Chip {
                    required property string modelData
                    text: modelData
                    on: items.row.value.indexOf(modelData) >= 0
                    onClicked: {
                        const now = items.row.value.slice()
                        const at = now.indexOf(modelData)
                        if (at >= 0) now.splice(at, 1); else now.push(modelData)
                        root.change(items.row, now)
                    }
                }
            }
        }
    }
    Component {
        id: numberEditor
        RowLayout {
            id: number
            property var row: parent ? parent.row : null
            property real step: number.row && number.row.kind === "float" ? (number.row.max <= 2 ? 0.1 : 1) : 1
            property real pending: number.row ? number.row.value : 0
            spacing: 6
            function bump(by) {
                const lo = number.row.min === null ? -1e9 : number.row.min
                const hi = number.row.max === null ? 1e9 : number.row.max
                number.pending = Math.min(hi, Math.max(lo, Math.round((number.pending + by) * 10) / 10))
                apply.restart()  // one change after the clicks stop, not one per click
            }
            Timer { id: apply; interval: 900; onTriggered: root.change(number.row, number.pending) }
            Chip { text: "−"; onClicked: number.bump(-number.step) }
            Label { text: String(number.pending); Layout.minimumWidth: 48; horizontalAlignment: Text.AlignHCenter }
            Chip { text: "+"; onClicked: number.bump(number.step) }
        }
    }
    Component {
        id: textEditor
        Rectangle {
            id: field
            property var row: parent ? parent.row : null
            Layout.maximumWidth: 420
            implicitWidth: 420
            implicitHeight: input.implicitHeight + 12
            radius: root.radius
            color: root.c.bg1
            border.width: 1
            border.color: input.activeFocus ? root.c.accentMid : root.c.border
            TextInput {
                id: input
                anchors { left: parent.left; right: parent.right; verticalCenter: parent.verticalCenter; margins: 8 }
                text: field.row && field.row.value !== null ? String(field.row.value) : ""
                color: root.c.fg; font.family: root.font; font.pixelSize: 13
                selectByMouse: true
                onAccepted: root.change(field.row, text === "" ? null : text)  // Enter applies
            }
        }
    }
}
