// The 3D preview: the model on a dark stage, turning slowly; drag to orbit, wheel to zoom.
import QtQuick
import QtQuick3D
import QtQuick3D.Helpers
import QtQuick3D.AssetUtils

Rectangle {
    id: root
    color: "#04070c"

    View3D {
        id: view
        anchors.fill: parent
        environment: SceneEnvironment {
            backgroundMode: SceneEnvironment.Color
            clearColor: "#04070c"
            antialiasingMode: SceneEnvironment.MSAA
            antialiasingQuality: SceneEnvironment.High
        }

        Node {
            id: origin
            PerspectiveCamera {
                id: camera
                position: Qt.vector3d(0, 40, 320)
                eulerRotation.x: -7
                clipNear: 1
                clipFar: 20000
            }
        }

        DirectionalLight { eulerRotation.x: -35; eulerRotation.y: -30; brightness: 1.25 }
        DirectionalLight { eulerRotation.x: 15; eulerRotation.y: 150; brightness: 0.55; color: "#9fdcff" }
        DirectionalLight { eulerRotation.x: 70; brightness: 0.25; color: "#22d3ee" }

        Node {
            id: turntable
            RuntimeLoader {
                id: loader
                source: modelUrl
                onStatusChanged: if (status === RuntimeLoader.Success) root.fit()
            }
            NumberAnimation on eulerRotation.y {
                id: spin
                from: 0; to: 360; duration: 16000; loops: Animation.Infinite
            }
        }

        OrbitCameraController {
            anchors.fill: parent
            origin: origin
            camera: camera
        }
    }

    function fit() {
        var lo = loader.bounds.minimum
        var hi = loader.bounds.maximum
        var size = Math.max(hi.x - lo.x, hi.y - lo.y, hi.z - lo.z)
        if (!(size > 0)) return
        var s = 200 / size
        loader.scale = Qt.vector3d(s, s, s)
        loader.position = Qt.vector3d(-(lo.x + hi.x) / 2 * s, -(lo.y + hi.y) / 2 * s,
                                      -(lo.z + hi.z) / 2 * s)
    }

    Text {
        anchors.centerIn: parent
        visible: loader.status !== RuntimeLoader.Success
        color: loader.status === RuntimeLoader.Error ? "#ff4d5e" : "#80a6ba"
        text: loader.status === RuntimeLoader.Error
              ? "Couldn't show this model: " + loader.errorString
              : "Loading the model…"
        font.pixelSize: 14
        wrapMode: Text.Wrap
        width: parent.width * 0.8
        horizontalAlignment: Text.AlignHCenter
    }
}
