pragma ComponentBehavior: Bound
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Item {
    id: root
    property var snapshot: ({mode: "idle", windows: [], workspaces: [], selected: 0})
    property var iconResolver: app => ""
    property var appNameResolver: app => app
    property color accent: "#e5b383"
    property color foreground: "#f3ece5"
    property color muted: "#c9bdb0"
    readonly property int columns: 1
    readonly property var windows: snapshot.windows || []
    readonly property bool desktopOpen: snapshot.mode === "desktop" || snapshot.mode === "closing"
    readonly property real rowHeight: height < 480 ? 58 : 72
    property bool cardsFocused: false
    signal action(var message)
    focus: desktopOpen
    onActiveFocusChanged: if (activeFocus) cardsFocused = false
    onSnapshotChanged: if (snapshot.loading && cardsFocused) forceActiveFocus()
    Component.onCompleted: send({command: "layout", columns: columns})

    function send(message) { action(message); }
    function focusCard(index) {
        if (!windows.length || snapshot.loading) return;
        index = (index + windows.length) % windows.length;
        windowList.positionViewAtIndex(index, ListView.Contain);
        windowList.forceLayout();
        const row = windowList.itemAtIndex(index);
        if (row) row.forceActiveFocus(Qt.TabFocusReason);
    }
    Keys.onTabPressed: event => { root.focusCard(0); event.accepted = true; }
    Keys.onBacktabPressed: event => { root.focusCard(-1); event.accepted = true; }
    Keys.onPressed: event => {
        if (event.key === Qt.Key_Escape) root.send({command: "cancel"});
        else if (root.snapshot.loading) { event.accepted = true; return; }
        else {
            const directions = { [Qt.Key_Left]: "left", [Qt.Key_Right]: "right", [Qt.Key_Up]: "up", [Qt.Key_Down]: "down" };
            if (directions[event.key]) root.send({command: "navigate", direction: directions[event.key]});
            else if (event.key === Qt.Key_Return || event.key === Qt.Key_Enter) root.send({command: "confirm"});
            else if (event.key === Qt.Key_PageUp) root.send({command: "workspace", delta: -1});
            else if (event.key === Qt.Key_PageDown) root.send({command: "workspace", delta: 1});
            else return;
        }
        event.accepted = true;
    }

    Rectangle {
        anchors.fill: parent
        color: "#55120f0c"
        MouseArea { anchors.fill: parent; onClicked: root.send({command: "cancel"}) }
    }
    Rectangle {
        id: panel
        objectName: "overlayPanel"
        anchors.centerIn: parent
        width: Math.min(parent.width - (parent.width < 480 ? 24 : 48), 760)
        height: Math.min(parent.height - 24, 560, content.implicitHeight + padding * 2)
        color: "#28231f"
        border.color: "#887767"
        radius: 12
        readonly property bool narrow: width < 420
        readonly property bool short: root.height < 480
        readonly property real padding: short || narrow ? 12 : 22
        readonly property real gap: short ? 7 : 12
        MouseArea { anchors.fill: parent }

        ColumnLayout {
            id: content
            anchors.fill: parent
            anchors.margins: panel.padding
            spacing: panel.gap

            RowLayout {
                Layout.fillWidth: true
                Text {
                    Layout.fillWidth: true
                    Layout.minimumWidth: 0
                    text: "Workspace " + (root.snapshot.workspace ? root.snapshot.workspace.name : "")
                    textFormat: Text.PlainText
                    color: root.foreground
                    font.pixelSize: panel.short ? 22 : 26
                    font.weight: Font.DemiBold
                    elide: Text.ElideRight
                }
                Text { text: root.snapshot.monitor || ""; color: root.muted; font.pixelSize: 13 }
            }

            RowLayout {
                Layout.fillWidth: true
                spacing: panel.narrow ? 8 : 14
                ControllerButton { control: "previousWorkspace"; size: panel.short ? 24 : 28 }
                Flickable {
                    id: workspaceStrip
                    Layout.fillWidth: true
                    Layout.minimumWidth: 0
                    implicitHeight: panel.short ? 32 : 38
                    contentWidth: workspaceRow.width
                    contentHeight: height
                    clip: true
                    flickableDirection: Flickable.HorizontalFlick
                    function revealCurrent() {
                        const index = (root.snapshot.workspaces || []).findIndex(w => root.snapshot.workspace && w.name === root.snapshot.workspace.name);
                        const item = workspaceRepeater.itemAt(index);
                        if (item) contentX = Math.max(0, Math.min(Math.max(0, contentWidth - width), item.x + item.width / 2 - width / 2));
                    }
                    onWidthChanged: Qt.callLater(revealCurrent)
                    Connections { target: root; function onSnapshotChanged() { Qt.callLater(workspaceStrip.revealCurrent); } }
                    Row {
                        id: workspaceRow
                        spacing: panel.narrow ? 8 : 10
                        Repeater {
                            id: workspaceRepeater
                            model: root.snapshot.workspaces || []
                            delegate: Item {
                                id: workspaceTile
                                required property var modelData
                                readonly property bool current: root.snapshot.workspace && root.snapshot.workspace.name === modelData.name
                                width: Math.max(34, label.implicitWidth + 18,
                                                (workspaceStrip.width - workspaceRow.spacing * Math.max(0, workspaceRepeater.count - 1)) / Math.max(1, workspaceRepeater.count))
                                height: workspaceStrip.height
                                Text {
                                    id: label
                                    anchors.horizontalCenter: parent.horizontalCenter
                                    anchors.top: parent.top
                                    text: workspaceTile.modelData.name
                                    textFormat: Text.PlainText
                                    font.pixelSize: panel.short ? 18 : 21
                                    font.weight: workspaceTile.current ? Font.Bold : Font.Normal
                                    color: workspaceTile.current ? root.accent : root.muted
                                }
                                Rectangle {
                                    anchors.horizontalCenter: parent.horizontalCenter
                                    anchors.bottom: parent.bottom
                                    width: Math.min(parent.width - 12, 30); height: 3; radius: 1.5
                                    color: root.accent; visible: workspaceTile.current
                                }
                            }
                        }
                    }
                }
                ControllerButton { control: "nextWorkspace"; size: panel.short ? 24 : 28 }
            }

            Item {
                Layout.fillWidth: true
                Layout.fillHeight: true
                Layout.minimumHeight: root.windows.length ? root.rowHeight : 76
                implicitHeight: root.windows.length ? Math.min(root.windows.length, 5) * root.rowHeight + Math.min(root.windows.length - 1, 4) * 6 : 88
                ListView {
                    id: windowList
                    objectName: "windowList"
                    anchors.fill: parent
                    visible: root.windows.length > 0
                    enabled: !root.snapshot.loading
                    model: root.windows
                    spacing: 6
                    clip: true
                    currentIndex: root.snapshot.selected || 0
                    onCurrentIndexChanged: {
                        positionViewAtIndex(currentIndex, ListView.Contain);
                        if (root.cardsFocused && currentItem) currentItem.forceActiveFocus();
                    }
                    ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded; focusPolicy: Qt.NoFocus }
                    delegate: Rectangle {
                        id: windowRow
                        required property var modelData
                        required property int index
                        readonly property bool selected: index === root.snapshot.selected
                        readonly property string appName: root.appNameResolver(modelData.app)
                        width: windowList.width
                        height: root.rowHeight
                        radius: 7
                        color: selected ? "#49372b" : "transparent"
                        border.width: activeFocus ? 3 : 1
                        border.color: selected || activeFocus ? root.accent : "transparent"
                        activeFocusOnTab: true
                        Accessible.role: Accessible.Button
                        Accessible.name: modelData.title + ", " + appName
                        Accessible.onPressAction: root.send({command: "select", index: windowRow.index})
                        onActiveFocusChanged: if (activeFocus) {
                            root.cardsFocused = true;
                            windowList.positionViewAtIndex(index, ListView.Contain);
                            root.send({command: "highlight", index: index});
                        }
                        Keys.onTabPressed: event => { root.focusCard(windowRow.index + 1); event.accepted = true; }
                        Keys.onBacktabPressed: event => { root.focusCard(windowRow.index - 1); event.accepted = true; }
                        Keys.onPressed: event => {
                            if (event.key === Qt.Key_Return || event.key === Qt.Key_Enter || event.key === Qt.Key_Space) {
                                root.send({command: "select", index: windowRow.index});
                                event.accepted = true;
                            }
                        }
                        RowLayout {
                            anchors.fill: parent
                            anchors.margins: panel.short ? 10 : 14
                            spacing: panel.narrow ? 12 : 18
                            WindowIcon {
                                Layout.preferredWidth: panel.short ? 30 : 40
                                Layout.preferredHeight: Layout.preferredWidth
                                source: root.iconResolver(windowRow.modelData.app)
                                fallbackColor: root.muted
                            }
                            ColumnLayout {
                                Layout.fillWidth: true
                                Layout.minimumWidth: 0
                                spacing: 3
                                Text {
                                    Layout.fillWidth: true
                                    text: windowRow.modelData.title
                                    textFormat: Text.PlainText
                                    color: root.foreground
                                    font.pixelSize: panel.short || panel.narrow ? 17 : 21
                                    font.weight: windowRow.selected ? Font.DemiBold : Font.Normal
                                    elide: Text.ElideRight
                                }
                                Text {
                                    Layout.fillWidth: true
                                    text: windowRow.modelData.active ? "Current window" : windowRow.appName
                                    textFormat: Text.PlainText
                                    visible: text.length > 0 && text !== windowRow.modelData.title
                                    color: root.muted
                                    font.pixelSize: panel.short ? 12 : 14
                                    elide: Text.ElideRight
                                }
                            }
                        }
                        MouseArea { anchors.fill: parent; onClicked: root.send({command: "select", index: windowRow.index}) }
                    }
                }
                Column {
                    anchors.centerIn: parent
                    width: parent.width
                    visible: root.windows.length === 0
                    spacing: 8
                    Text {
                        width: parent.width
                        horizontalAlignment: Text.AlignHCenter
                        wrapMode: Text.WordWrap
                        text: root.snapshot.loading ? "Updating windows…" : "This workspace is empty"
                        color: root.foreground
                        font.pixelSize: panel.short ? 18 : 22
                    }
                    Text {
                        width: parent.width
                        visible: !root.snapshot.loading
                        horizontalAlignment: Text.AlignHCenter
                        wrapMode: Text.WordWrap
                        text: "Use LB / RB to find another workspace"
                        color: root.muted
                        font.pixelSize: panel.short ? 13 : 15
                    }
                }
            }

            Rectangle { Layout.fillWidth: true; implicitHeight: 1; color: "#756454" }
            Flow {
                Layout.fillWidth: true
                Layout.minimumWidth: 0
                spacing: panel.narrow ? 6 : 18
                ControllerHint { visible: root.windows.length > 0; control: "confirm"; caption: panel.narrow ? "Focus" : "Focus window"; foreground: root.foreground; size: panel.short ? 24 : 28; textSize: panel.short || panel.narrow ? 13 : 16 }
                ControllerHint { control: "back"; caption: root.snapshot.input_mode !== "bridge" ? "Close" : panel.narrow ? "Mouse" : "Mouse mode"; foreground: root.foreground; size: panel.short ? 24 : 28; textSize: panel.short || panel.narrow ? 13 : 16 }
                ControllerHint { visible: root.snapshot.input_mode === "bridge"; control: "keyboard"; caption: "Keyboard"; foreground: root.foreground; size: panel.short ? 24 : 28; textSize: panel.short || panel.narrow ? 13 : 16 }
            }
            Item {
                Layout.fillWidth: true
                implicitHeight: Math.max(instruction.height, device.y + device.height)
                Text {
                    id: instruction
                    anchors.top: parent.top
                    width: panel.narrow ? parent.width : Math.max(0, parent.width - device.width - 16)
                    text: root.snapshot.mode === "closing" ? "Release controls to resume gameplay" :
                          root.snapshot.view_pending ? "Release controls to continue" :
                          (root.snapshot.activation_hint || "L3 + R3 · Hold") + (root.snapshot.input_mode === "bridge" ? " and release to play" : " and release to open")
                    wrapMode: Text.WordWrap
                    color: root.muted
                    font.pixelSize: panel.short ? 11 : 13
                }
                Text {
                    id: device
                    anchors.right: parent.right
                    anchors.top: panel.narrow ? instruction.bottom : parent.top
                    anchors.topMargin: panel.narrow ? 3 : 0
                    width: panel.narrow ? parent.width : Math.min(implicitWidth, parent.width * 0.32)
                    text: root.snapshot.controller || "Controller"
                    textFormat: Text.PlainText
                    color: root.muted
                    font.pixelSize: panel.short ? 11 : 12
                    elide: Text.ElideRight
                }
            }
        }
    }
}
