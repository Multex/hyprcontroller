import QtQuick
import "ControllerLayout.js" as ControllerLayout

Rectangle {
    id: root
    property string control: "confirm"
    property real size: 30
    readonly property var visual: ControllerLayout.buttons[control] || {
        label: control, color: "#181818", foreground: "#f1f1f1", round: false
    }
    implicitWidth: visual.round ? size : Math.max(size + 12, label.implicitWidth + 14)
    implicitHeight: size
    radius: visual.round ? height / 2 : 5
    color: visual.color
    border.color: visual.round && control !== "windows" ? visual.color : "#858585"
    border.width: 1
    Text {
        id: label
        anchors.centerIn: parent
        text: root.visual.label
        color: root.visual.foreground
        font.pixelSize: root.size * 0.55
        font.weight: Font.Bold
    }
}
