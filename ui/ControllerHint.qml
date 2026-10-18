import QtQuick

Row {
    id: root
    property string control: "confirm"
    property string caption: ""
    property real size: 30
    property real textSize: 17
    property color foreground: "#f1f1f1"
    spacing: 9
    ControllerButton { control: root.control; size: root.size }
    Text {
        anchors.verticalCenter: parent.verticalCenter
        text: root.caption
        color: root.foreground
        font.pixelSize: root.textSize
    }
}
