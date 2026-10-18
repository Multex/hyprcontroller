//@ pragma UseQApplication
//@ pragma Env QS_NO_RELOAD_POPUP=1
import QtQuick
import QtQuick.Controls
import Quickshell
import Quickshell.Io
import Quickshell.Wayland

ShellRoot {
    id: root
    property var state: ({ mode: "idle", windows: [], workspaces: [], selected: 0, controller: "", monitor: "", error: "" })
    property string toast: ""
    property color accent: "#e5b383"
    property color foreground: "#f3ece5"
    property color muted: "#c9bdb0"
    property bool desktopOpen: state.mode === "desktop" || state.mode === "closing"
    property bool switcherOpen: desktopOpen && state.view !== "pointer"
    property var chosenScreen: {
        const screens = Quickshell.screens;
        return screens.find(s => s.name === state.monitor) || screens[0] || null;
    }

    function send(command) {
        backend.write(JSON.stringify(command) + "\n");
    }

    function windowIcon(appId) {
        const name = /^steam_app_\d+$/i.test(appId) ? "steam" : appId;
        const entry = DesktopEntries.heuristicLookup(name);
        return Quickshell.iconPath(entry && entry.icon ? entry.icon : name, true);
    }

    function windowAppName(appId) {
        const name = /^steam_app_\d+$/i.test(appId) ? "steam" : appId;
        const entry = DesktopEntries.heuristicLookup(name);
        return entry ? entry.name : "";
    }

    Process {
        id: backend
        command: [Quickshell.env("HYPRCONTROLLER_PYTHON") || "python3", "-m", "hyprcontroller.backend"]
        running: true
        stdinEnabled: true
        stdout: SplitParser {
            onRead: data => {
                try {
                    const next = JSON.parse(data);
                    if (next.type !== "state") return;
                    if (next.error && (next.error_sequence !== root.state.error_sequence || next.error !== root.state.error)) {
                        root.toast = next.error;
                        toastTimer.restart();
                    }
                    root.state = next;
                } catch (error) {
                    console.error("Invalid backend response:", error);
                }
            }
        }
        stderr: SplitParser { onRead: data => console.warn(data) }
        onStarted: root.send({command: "heartbeat"})
        onExited: (exitCode, exitStatus) => {
            const closeKeyboard = root.state.keyboard_used;
            const keyboardConfig = root.state.keyboard_config || "end4-pC";
            root.state = ({mode: "idle", windows: [], workspaces: [], selected: 0});
            if (closeKeyboard) {
                keyboardCleanup.command = ["qs", "ipc", "-c", keyboardConfig, "call", "osk", "close"];
                keyboardCleanup.running = true;
                cleanupTimeout.restart();
            } else Qt.quit();
        }
    }

    Process {
        id: keyboardCleanup
        onExited: Qt.quit()
    }
    Timer {
        id: cleanupTimeout
        interval: 2000
        onTriggered: { keyboardCleanup.running = false; Qt.quit(); }
    }

    Timer {
        interval: 500
        running: backend.running
        repeat: true
        onTriggered: root.send({command: "heartbeat"})
    }
    Timer { id: toastTimer; interval: 6000; onTriggered: root.toast = "" }

    PanelWindow {
        id: overlay
        screen: root.chosenScreen
        visible: root.switcherOpen && root.chosenScreen !== null
        anchors { top: true; bottom: true; left: true; right: true }
        exclusiveZone: 0
        color: "transparent"
        WlrLayershell.namespace: "hyprcontroller"
        WlrLayershell.layer: WlrLayer.Overlay
        WlrLayershell.keyboardFocus: WlrKeyboardFocus.Exclusive
        onVisibleChanged: if (visible) {
            root.send({command: "layout", columns: desktopView.columns});
            root.send({command: "visible"});
        }

        DesktopView {
            id: desktopView
            anchors.fill: parent
            snapshot: root.state
            iconResolver: appId => root.windowIcon(appId)
            appNameResolver: appId => root.windowAppName(appId)
            onAction: message => root.send(message)
        }
    }

    PanelWindow {
        id: desktopIndicator
        screen: root.chosenScreen
        visible: root.desktopOpen && !root.switcherOpen && root.chosenScreen !== null
        anchors { top: true; right: true }
        margins { top: 14; right: 14 }
        implicitWidth: Math.min(root.chosenScreen ? root.chosenScreen.width - 28 : 400, 420)
        implicitHeight: indicator.implicitHeight + 24
        exclusiveZone: 0
        color: "transparent"
        mask: Region {}
        WlrLayershell.namespace: "hyprcontroller-desktop"
        WlrLayershell.layer: WlrLayer.Overlay
        WlrLayershell.keyboardFocus: WlrKeyboardFocus.None
        Rectangle {
            anchors.fill: parent; radius: 8; color: "#f528231f"; border.color: "#887767"
            Column {
                id: indicator
                anchors.centerIn: parent
                width: parent.width - 28
                spacing: 8
                Text {
                    width: parent.width
                    text: root.state.mode === "closing" ? "Release controls to resume gameplay" :
                          root.state.view_pending ? "Release controls to continue" : "Desktop mode"
                    wrapMode: Text.WordWrap
                    color: root.foreground; font.pixelSize: 16; font.weight: Font.DemiBold
                }
                Flow {
                    width: parent.width; spacing: 12
                    ControllerHint { control: "confirm"; caption: "Click"; size: 26; textSize: 14 }
                    ControllerHint { control: "back"; caption: "Right click"; size: 26; textSize: 14 }
                    ControllerHint { control: "keyboard"; caption: "Keyboard"; size: 26; textSize: 14 }
                    ControllerHint { control: "windows"; caption: "Windows"; size: 26; textSize: 14 }
                }
                Text {
                    width: parent.width
                    text: "LB / RB: workspaces\n" + (root.state.activation_hint || "L3 + R3 · Hold") + " and release to play"
                    wrapMode: Text.WordWrap
                    color: root.muted; font.pixelSize: 14
                }
            }
        }
    }

    PanelWindow {
        id: toastWindow
        screen: root.chosenScreen
        visible: (root.toast !== "" || root.state.mode === "arming") && root.chosenScreen !== null
        anchors { bottom: true }
        margins.bottom: 36
        implicitWidth: Math.min(root.chosenScreen ? root.chosenScreen.width - 40 : 600, 660)
        implicitHeight: message.implicitHeight + 36
        exclusiveZone: 0
        color: "transparent"
        WlrLayershell.namespace: "hyprcontroller-notice"
        WlrLayershell.layer: WlrLayer.Overlay
        WlrLayershell.keyboardFocus: WlrKeyboardFocus.None
        Rectangle {
            anchors.fill: parent; radius: 8; color: "#f528231f"; border.color: "#887767"
            Text {
                id: message
                anchors.centerIn: parent
                width: parent.width - 40
                text: root.state.mode === "arming" ? "Release the buttons and center the sticks to open desktop mode" : root.toast
                wrapMode: Text.WordWrap
                horizontalAlignment: Text.AlignHCenter
                color: root.foreground; font.pixelSize: 17
            }
        }
    }
}
