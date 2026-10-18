import QtQuick

Item {
    id: root
    property string source: ""
    property color fallbackColor: "#b8b8b8"
    Image {
        id: image
        anchors.fill: parent
        source: root.source
        fillMode: Image.PreserveAspectFit
        sourceSize.width: width
        sourceSize.height: height
        visible: status === Image.Ready
    }
    Rectangle {
        anchors.centerIn: parent
        width: parent.width * 0.72
        height: parent.height * 0.6
        color: "transparent"
        border.color: root.fallbackColor
        border.width: 2
        radius: 3
        visible: image.status !== Image.Ready
        Rectangle {
            x: 2; y: parent.height * 0.23
            width: parent.width - 4; height: 2
            color: root.fallbackColor
        }
    }
}
