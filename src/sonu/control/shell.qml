// SONU's Control Center (D90, parts 2 and 3): its own Quickshell window (`sonu control` starts `qs -n -p <this>`),
// so the owner's shell and dotfiles are never touched. Every change goes through `sonu settings set` — the
// same checks as the terminal: only the catalogued settings, the whole file validated before it's written,
// a change that loosens privacy or safety asked here first, every change audited, sonud restarted once idle.
// "Your data" shows what SONU keeps about the user (memory, conversations, the journal, the audit log) through
// `sonu memory / conversations / journal / audit`; deleting is always asked here first.
// Colours, corners and font come from the rice's colors.json, live.
import QtQuick
import QtQuick.Layouts
import Quickshell
import Quickshell.Io

ShellRoot {
    id: root

    readonly property string sonu: Quickshell.env("SONU_BIN") || "sonu"
    readonly property var groups: ["Brain", "Voice", "Proactive", "Privacy", "Look"]
    readonly property var dataPages: ["Memory", "Conversations", "Activity", "Audit log"]
    // the page shown: a settings group or one of "your data" (SONU_CONTROL_PAGE: open on that page)
    property string group: Quickshell.env("SONU_CONTROL_PAGE") || "Brain"
    Component.onCompleted: loadData()
    readonly property bool onData: root.dataPages.indexOf(root.group) >= 0
    property string when: "today"    // conversations and activity
    property var found: null         // what the data page shows (parsed from `sonu … --json`)
    property var rows: []
    property var models: []          // installed local models that can plan: { name, gb }
    property string status: ""
    property bool busy: false
    property var asking: null        // { title, text, yes, act } waiting for "are you sure?"

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
        if (loosens(row, v)) {
            root.asking = {
                title: row.label + " → " + shown(v) + "?",
                text: row.help + " This gives SONU more reach or keeps more of your data.",
                yes: "Yes, change it",
                act: () => root.set(row, v, true)
            }
            return
        }
        set(row, v, false)
    }
    function set(row, v, confirmed) {
        const command = [root.sonu, "settings", "set", row.key, arg(row, v)]
        if (confirmed) command.push("--yes")  // only after the user said yes here
        run(command, "Changing " + row.label + "…")
    }
    function run(command, doing) {
        root.busy = true
        root.status = doing
        setter.command = command
        setter.running = true
    }
    function sure(title, text, yes, command) {  // deleting: always asked here first
        root.asking = { title: title, text: text, yes: yes, act: () => root.run(command, "Working…") }
    }
    function reload() { lister.running = true }
    function loadData() {
        if (!root.onData) return
        root.found = null
        const s = root.sonu
        dataReader.command = root.group === "Memory" ? [s, "memory", "list", "--json"]
            : root.group === "Conversations" ? [s, "conversations", "list", root.when === "this week" ? "this_week" : root.when, "--json"]
            : root.group === "Activity" ? [s, "journal", "show", root.when === "this week" ? "this_week" : root.when, "--json"]
            : [s, "audit", "tail", "-n", "60", "--json"]
        dataReader.running = true
    }

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
        id: dataReader
        stdout: StdioCollector {
            onStreamFinished: {
                try {
                    root.found = root.group === "Audit log"
                        ? text.trim().split("\n").filter(l => l).map(l => JSON.parse(l)).reverse()
                        : JSON.parse(text)
                } catch (e) { root.found = null }
            }
        }
        stderr: StdioCollector { onStreamFinished: if (text.trim()) root.status = text.trim().replace(/\[[^\]]*\]/g, "") }
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
            root.loadData()
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
                    Text {
                        text: "YOUR DATA"
                        color: root.c.accentDim; font.family: root.font; font.pixelSize: 10
                        Layout.topMargin: 14; Layout.leftMargin: 10
                    }
                    Repeater {
                        model: root.dataPages
                        delegate: Rectangle {
                            required property string modelData
                            Layout.fillWidth: true
                            implicitHeight: 32
                            radius: root.radius
                            color: root.group === modelData ? root.c.bg3 : (dataHover.containsMouse ? root.c.bg2 : "transparent")
                            Text {
                                anchors { left: parent.left; leftMargin: 10; verticalCenter: parent.verticalCenter }
                                text: modelData
                                color: root.group === modelData ? root.c.accentBright : root.c.fg
                                font.family: root.font; font.pixelSize: 13
                            }
                            MouseArea {
                                id: dataHover
                                anchors.fill: parent
                                hoverEnabled: true
                                cursorShape: Qt.PointingHandCursor
                                onClicked: { root.group = modelData; root.when = "today"; root.loadData() }
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
                    visible: root.onData
                    clip: true
                    contentHeight: dataBody.implicitHeight + 28
                    ColumnLayout {
                        id: dataBody
                        anchors { left: parent.left; right: parent.right; top: parent.top; margins: 18 }
                        spacing: 12

                        Text {
                            Layout.fillWidth: true
                            text: root.group === "Memory" ? "What you asked SONU to remember (“remember …”). Only on this laptop."
                                : root.group === "Conversations" ? "What you and SONU said to each other. Only on this laptop; kept " + "for the days set under Privacy."
                                : root.group === "Activity" ? "Which windows you used (the activity journal). Only on this laptop."
                                : "What SONU did and why — the latest 60 records. Private results are never in it."
                            color: root.c.accentDim; font.family: root.font; font.pixelSize: 11; wrapMode: Text.Wrap
                        }
                        // when (conversations, activity)
                        Flow {
                            Layout.fillWidth: true
                            visible: root.group === "Conversations" || root.group === "Activity"
                            spacing: 6
                            Repeater {
                                model: root.group === "Conversations" ? ["today", "yesterday", "this week", "all"] : ["today", "yesterday", "this week"]
                                delegate: Chip {
                                    required property string modelData
                                    text: modelData
                                    on: root.when === modelData
                                    onClicked: { root.when = modelData; root.loadData() }
                                }
                            }
                        }
                        // what can be done here
                        Flow {
                            Layout.fillWidth: true
                            spacing: 6
                            visible: root.group !== "Audit log"
                            Chip {
                                visible: root.group === "Memory"
                                text: "Forget everything"
                                onClicked: root.sure("Forget everything SONU remembers?", "Your aliases, your name and your notes go. It can't be undone.", "Forget it all", [root.sonu, "memory", "forget", "--all", "--yes"])
                            }
                            Chip {
                                visible: root.group === "Conversations"
                                text: "Delete the last hour"
                                onClicked: root.sure("Delete the last hour of conversations?", "It can't be undone.", "Delete", [root.sonu, "conversations", "forget", "--since", "last_hour", "--yes"])
                            }
                            Chip {
                                visible: root.group === "Conversations"
                                text: "Delete today"
                                onClicked: root.sure("Delete today's conversations?", "It can't be undone.", "Delete", [root.sonu, "conversations", "forget", "--since", "today", "--yes"])
                            }
                            Chip {
                                visible: root.group === "Conversations"
                                text: "Delete everything"
                                onClicked: root.sure("Delete all your conversations with SONU?", "It can't be undone.", "Delete all", [root.sonu, "conversations", "forget", "--since", "everything", "--yes"])
                            }
                            Chip {
                                visible: root.group === "Activity"
                                text: root.found && root.found.paused ? "Resume recording" : "Pause recording"
                                on: root.found !== null && root.found.paused === true
                                onClicked: root.run([root.sonu, "journal", root.found && root.found.paused ? "resume" : "pause"], "…")
                            }
                            Chip {
                                visible: root.group === "Activity"
                                text: "Delete the last hour"
                                onClicked: root.sure("Delete the last hour of the journal?", "It can't be undone.", "Delete", [root.sonu, "journal", "forget", "--hours", "1"])
                            }
                            Chip {
                                visible: root.group === "Activity"
                                text: "Delete everything"
                                onClicked: root.sure("Delete the whole activity journal?", "It can't be undone.", "Delete all", [root.sonu, "journal", "forget", "--all"])
                            }
                        }
                        Text {
                            visible: root.found === null
                            text: "Reading…"
                            color: root.c.accentMid; font.family: root.font; font.pixelSize: 12
                        }
                        Text {
                            visible: root.found !== null && root.found.length === 0
                            text: root.group === "Memory" ? "SONU doesn't remember anything about you yet." : "Nothing here."
                            color: root.c.accentMid; font.family: root.font; font.pixelSize: 12
                        }
                        // the activity journal's summary
                        Label {
                            Layout.fillWidth: true
                            visible: root.group === "Activity" && root.found !== null
                            text: root.found && root.found.summary ? root.found.summary : ""
                            wrapMode: Text.Wrap
                        }
                        // memory, conversations, the audit log: one item each
                        Repeater {
                            model: root.group !== "Activity" && root.found && root.found.length ? root.found : []
                            delegate: Rectangle {
                                id: item
                                required property var modelData
                                Layout.fillWidth: true
                                implicitHeight: itemBody.implicitHeight + 16
                                radius: root.radius
                                color: root.c.bg1
                                ColumnLayout {
                                    id: itemBody
                                    anchors { left: parent.left; right: parent.right; top: parent.top; margins: 8 }
                                    spacing: 3
                                    RowLayout {
                                        Layout.fillWidth: true
                                        Text {
                                            text: root.group === "Memory" ? item.modelData.kind
                                                : root.group === "Conversations" ? item.modelData.time
                                                : Qt.formatDateTime(new Date(item.modelData.ts), "ddd HH:mm") + " · " + item.modelData.kind
                                            color: root.c.accentDim; font.family: root.font; font.pixelSize: 11
                                        }
                                        Item { Layout.fillWidth: true }
                                        Chip {
                                            visible: root.group === "Memory"
                                            text: "Forget"
                                            onClicked: root.sure("Forget this?", item.modelData.text, "Forget it", [root.sonu, "memory", "forget", String(item.modelData.n), "--yes"])
                                        }
                                    }
                                    Label {
                                        Layout.fillWidth: true
                                        text: root.group === "Memory" ? item.modelData.text
                                            : root.group === "Conversations" ? "“" + item.modelData.request + "”"
                                            : JSON.stringify(item.modelData.data)
                                        font.pixelSize: root.group === "Audit log" ? 11 : 13
                                        wrapMode: Text.Wrap; maximumLineCount: 3; elide: Text.ElideRight
                                    }
                                    Text {
                                        Layout.fillWidth: true
                                        visible: root.group === "Conversations"
                                        text: root.group === "Conversations" ? item.modelData.reply : ""
                                        color: root.c.accentMid; font.family: root.font; font.pixelSize: 12
                                        wrapMode: Text.Wrap; maximumLineCount: 4; elide: Text.ElideRight
                                    }
                                }
                            }
                        }
                    }
                }

                Flickable {
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    visible: !root.onData
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
                        text: root.status || (root.onData ? "Deleting always asks first, and is logged (how many — never what)."
                            : "Changes are checked, written to your config (comments kept) and logged; SONU restarts by itself to use them.")
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
                        text: root.asking ? root.asking.title : ""
                        font.bold: true; font.pixelSize: 15; wrapMode: Text.Wrap
                    }
                    Text {
                        Layout.fillWidth: true
                        text: root.asking ? root.asking.text : ""
                        color: root.c.accentMid; font.family: root.font; font.pixelSize: 12; wrapMode: Text.Wrap
                    }
                    RowLayout {
                        spacing: 8
                        Chip { text: root.asking ? root.asking.yes : ""; on: true; onClicked: { const a = root.asking; root.asking = null; a.act() } }
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
