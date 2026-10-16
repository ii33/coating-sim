pragma ComponentBehavior: Bound
import QtQuick
import QtQuick.Controls
import "."

AppWindow {
    id: window
    titleBase: "关于与帮助"
    minimumWidth: 900
    minimumHeight: 620
    property string internalVersion: "unknown"
    property string gitCommitLabel: "Git 提交：未记录（源码归档或未提供构建元数据）"
    property int page: 0
    readonly property var pageNames: ["关于", "软件说明书", "算法说明", "开源声明", "开源协议"]

    Rectangle {
        id: hero
        objectName: "aboutHero"
        x: 0
        y: 0
        width: parent.width
        height: Math.max(290, Math.min(375, parent.height * 0.43))
        gradient: Gradient {
            GradientStop { position: 0; color: "#103b4c" }
            GradientStop { position: 1; color: "#1a7279" }
        }
        Rectangle {
            width: Math.min(parent.width * 0.45, 600)
            height: width
            x: parent.width * 0.45
            y: -height * 0.62
            radius: width / 2
            color: "#16ffffff"
        }
    }

    Column {
        x: Math.max(34, window.width * 0.055)
        y: Math.max(85, hero.height * 0.32)
        width: Math.min(window.width * 0.43, 660)
        spacing: 13
        z: 2
        Text {
            width: parent.width
            text: "行星盘沉积仿真"
            color: "#f7fdff"
            font.pixelSize: Math.max(32, Math.min(53, window.width * 0.035))
            font.bold: true
            wrapMode: Text.WordWrap
        }
        Text {
            width: parent.width
            text: "探索沉积几何、运动与膜厚分布"
            color: "#d4f0ee"
            font.pixelSize: Math.max(17, Math.min(24, window.width * 0.015))
            wrapMode: Text.WordWrap
        }
        Row {
            spacing: 6
            Text {
                objectName: "aboutAuthorCredit"
                text: "Designed by ii33"
                color: "#b9dad9"
                font.pixelSize: 14
            }
            Text {
                objectName: "aboutBuildCredit"
                text: "Built with OpenAI Codex"
                color: "#b9dad9"
                font.pixelSize: 11
                anchors.verticalCenter: parent.verticalCenter
            }
        }
    }

    Rectangle {
        id: sheet
        objectName: "aboutContentSheet"
        x: Math.max(20, window.width * 0.03)
        y: hero.height - 30
        width: window.width - 2 * x
        height: window.height - y - 20
        z: 3
        radius: 20
        color: window.palette.base
        border.color: window.palette.mid
        border.width: 1

        Row {
            id: navigation
            x: 26
            y: 23
            spacing: 8
            Repeater {
                model: window.pageNames
                delegate: Rectangle {
                    id: tab
                    required property int index
                    required property string modelData
                    width: Math.max(104, navigationText.implicitWidth + 24)
                    height: 38
                    radius: 9
                    color: tab.index === window.page
                           ? window.palette.highlight : "transparent"
                    border.color: tab.index === window.page
                                  ? window.palette.highlight : window.palette.mid
                    Text {
                        id: navigationText
                        anchors.centerIn: parent
                        text: tab.modelData
                        color: tab.index === window.page
                               ? window.palette.highlightedText
                               : window.palette.windowText
                        font.pixelSize: 16
                        font.bold: tab.index === window.page
                    }
                    MouseArea {
                        anchors.fill: parent
                        cursorShape: Qt.PointingHandCursor
                        onClicked: window.page = tab.index
                    }
                }
            }
        }

        Loader {
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.top: navigation.bottom
            anchors.bottom: parent.bottom
            anchors.leftMargin: 28
            anchors.rightMargin: 28
            anchors.topMargin: 22
            anchors.bottomMargin: 20
            sourceComponent: window.page === 0 ? overviewPage
                           : window.page === 1 ? manualPage
                           : window.page === 2 ? algorithmsPage
                           : window.page === 3 ? openSourcePage
                                               : licensesPage
        }
    }

    Image {
        id: character
        z: 4
        x: window.width - width - Math.max(15, window.width * 0.025)
        readonly property real aspect: sourceSize.width > 0 && sourceSize.height > 0
                                       ? sourceSize.width / sourceSize.height : 1.75
        // Anchor the visible lower elbow edge just below the sheet top. The
        // source is 1672x941 and its lower edge has no transparent padding.
        // Both values are image-relative, so resizing keeps the same relation.
        readonly property real elbowAnchorY: 0.998
        readonly property real elbowOverlapRatio: 0.008
        width: Math.min(window.width * 0.44, 740,
                        Math.max(1, sheet.y /
                                 (elbowAnchorY - elbowOverlapRatio)) * aspect)
        height: width / aspect
        // A small optical offset keeps the elbows resting slightly farther
        // onto the sheet without changing the responsive scale or anchors.
        readonly property real visualOffsetY: 8
        y: sheet.y + height * elbowOverlapRatio - height * elbowAnchorY + visualOffsetY
        source: bridge.boardUrl
        fillMode: Image.PreserveAspectFit
        smooth: true
        mipmap: true
        asynchronous: true
    }

    Component {
        id: overviewPage
        ScrollView {
            id: overviewScroll
            clip: true
            ScrollBar.vertical.policy: ScrollBar.AsNeeded
            Column {
                width: Math.max(1, overviewScroll.availableWidth - 22)
                spacing: 14
                Label {
                    text: "关于项目"
                    font.pixelSize: 25
                    font.bold: true
                }
                Label { text: "内部版本：" + window.internalVersion }
                Label {
                    width: parent.width
                    text: window.gitCommitLabel
                    wrapMode: Text.WordWrap
                }
                Label {
                    width: parent.width
                    text: "本项目提供工程化的膜系设计、光谱仿真、膜系优化、行星盘沉积与薄膜外观预览。每个工程独立管理资源、窗口和未保存的修改；启动器提供最近工程和全局设置入口。"
                    wrapMode: Text.WordWrap
                }
                Label { text: "作者：ii33" }
                Label { text: "软件许可证：MIT · Copyright (c) 2026 ii33" }
                Label {
                    width: parent.width
                    text: "源码仓库：ii33/coating-sim"
                    wrapMode: Text.WordWrap
                }
                Label {
                    width: parent.width
                    text: "当前模型采用外啮合太阳轮与行星盘运动；输入速率与绝对膜厚需要真实设备测量校准。能力范围、使用流程和已知近似请查看软件说明书。"
                    wrapMode: Text.WordWrap
                }
            }
        }
    }

    Component {
        id: manualPage
        ScrollView {
            id: manualScroll
            clip: true
            ScrollBar.vertical.policy: ScrollBar.AsNeeded
            Column {
                width: Math.max(1, manualScroll.availableWidth - 22)
                spacing: 14
                Label {
                    text: "软件说明书"
                    font.pixelSize: 25
                    font.bold: true
                }
                Label {
                    width: parent.width
                    text: "从 .coatproj 工程文件进入膜系设计，通过窗口列表切换设计与仿真模块。外部资源先导入工程；保存写入当前工程。膜系设计、仿真与优化共享本工程的当前设计，参数随膜系保存。"
                    wrapMode: Text.WordWrap
                }
                Button {
                    text: "打开完整离线说明书"
                    onClicked: Qt.openUrlExternally(bridge.softwareManualUrl)
                }
                Label {
                    text: "按主题查看"
                    font.bold: true
                }
                Flow {
                    width: parent.width
                    spacing: 8
                    Repeater {
                        model: [
                            {title: "工程与资源", key: "projects"},
                            {title: "计算设备", key: "compute_devices"},
                            {title: "算法选型", key: "algorithm_selection"},
                            {title: "优化参数", key: "film_optimization_parameters"},
                            {key: "overview", title: "软件能力"},
                            {key: "workflow", title: "使用流程"},
                            {key: "coordinates", title: "坐标与单位"},
                            {key: "source", title: "蒸发源"},
                            {key: "substrate", title: "基片"},
                            {key: "machine", title: "机器结构"},
                            {key: "process", title: "工艺与算法"},
                            {key: "mask", title: "修正板"},
                            {key: "cad", title: "CAD 与加工图"},
                            {key: "results", title: "设备与结果"},
                            {key: "assistant", title: "看板娘权限"},
                            {key: "limitations", title: "边界与近似"}
                        ]
                        delegate: Button {
                            required property var modelData
                            text: modelData.title
                            onClicked: Qt.openUrlExternally(bridge.manualSectionUrl(modelData.key))
                        }
                    }
                }
                Label {
                    width: parent.width
                    text: "二维轮廓交换与图纸标注用于修正板加工沟通。程序不控制真实设备，不输出光学面型；具体可用功能以接口、文件校验与说明书为准。"
                    wrapMode: Text.WordWrap
                }
            }
        }
    }

    Component {
        id: algorithmsPage
        ScrollView {
            id: algorithmsScroll
            clip: true
            ScrollBar.vertical.policy: ScrollBar.AsNeeded
            Column {
                width: Math.max(1, algorithmsScroll.availableWidth - 22)
                spacing: 13
                Label {
                    text: "算法说明"
                    font.pixelSize: 25
                    font.bold: true
                }
                Label {
                    width: parent.width
                    text: "全时长积分按每道膜层的真实沉积时长、设定速率和连续初相积分；快速算法在残余时长占比不超过设定阈值时，以完整材料点回归周期计算厚层空间分布。同源周期场可以复用。"
                    wrapMode: Text.WordWrap
                }
                Label {
                    width: parent.width
                    text: "薄层仍按真实时长计算。快速算法的阈值约束忽略的时间比例，不保证膜厚误差小于同一百分比。"
                    wrapMode: Text.WordWrap
                }
                Label {
                    width: parent.width
                    text: "详细公式、图解、实现位置、近似条件与参考来源收录在项目内离线算法文档。"
                    wrapMode: Text.WordWrap
                }
                Button {
                    text: "打开完整算法文档"
                    onClicked: Qt.openUrlExternally(bridge.algorithmDocUrl)
                }
            }
        }
    }

    Component {
        id: openSourcePage
        ScrollView {
            id: sourceScroll
            clip: true
            ScrollBar.vertical.policy: ScrollBar.AsNeeded
            Column {
                width: Math.max(1, sourceScroll.availableWidth - 22)
                spacing: 13
                Label {
                    text: "开源声明"
                    font.pixelSize: 25
                    font.bold: true
                }
                Label {
                    width: parent.width
                    text: "以下列出项目直接依赖及可选计算依赖所使用的开源项目。名称链接指向其官方许可出处；实际分发仍须核对安装版本与随包许可文件。"
                    wrapMode: Text.WordWrap
                }
                Label {
                    width: parent.width
                    text: "requirements.txt 中的直接依赖："
                    wrapMode: Text.WordWrap
                }
                Label {
                    width: parent.width
                    wrapMode: Text.WordWrap
                    textFormat: Text.RichText
                    text: '<a href="https://github.com/numpy/numpy/blob/main/LICENSE.txt">NumPy</a> · 主项目 BSD 3-Clause；发行包可能包含另行许可的组件。'
                    onLinkActivated: function(link) { Qt.openUrlExternally(link) }
                }
                Label {
                    width: parent.width
                    wrapMode: Text.WordWrap
                    textFormat: Text.RichText
                    text: '<a href="https://matplotlib.org/stable/project/license.html">Matplotlib</a> · Matplotlib License（基于 PSF 许可）。'
                    onLinkActivated: function(link) { Qt.openUrlExternally(link) }
                }
                Label {
                    width: parent.width
                    wrapMode: Text.WordWrap
                    textFormat: Text.RichText
                    text: '<a href="https://github.com/python-pillow/Pillow/blob/main/LICENSE">Pillow</a> · MIT-CMU License。'
                    onLinkActivated: function(link) { Qt.openUrlExternally(link) }
                }
                Label {
                    width: parent.width
                    wrapMode: Text.WordWrap
                    textFormat: Text.RichText
                    text: '<a href="https://doc.qt.io/qtforpython-6/">PySide6-Essentials</a> · Qt for Python 社区版 LGPLv3 / GPLv3，另有商业许可。'
                    onLinkActivated: function(link) { Qt.openUrlExternally(link) }
                }
                Label {
                    width: parent.width
                    wrapMode: Text.WordWrap
                    textFormat: Text.RichText
                    text: '<a href="https://github.com/modelcontextprotocol/python-sdk/blob/main/LICENSE">MCP Python SDK</a> · 官方仓库当前 LICENSE 为 MIT。'
                    onLinkActivated: function(link) { Qt.openUrlExternally(link) }
                }
                Label {
                    width: parent.width
                    wrapMode: Text.WordWrap
                    textFormat: Text.RichText
                    text: '<a href="https://github.com/pydantic/pydantic/blob/main/LICENSE">Pydantic</a> · MIT License。'
                    onLinkActivated: function(link) { Qt.openUrlExternally(link) }
                }
                Label {
                    width: parent.width
                    text: bridge.openSourceHtml
                    wrapMode: Text.WordWrap
                    textFormat: Text.RichText
                    onLinkActivated: function(link) { Qt.openUrlExternally(link) }
                }
                Label {
                    width: parent.width
                    text: "可选计算依赖："
                    wrapMode: Text.WordWrap
                }
                Label {
                    width: parent.width
                    wrapMode: Text.WordWrap
                    textFormat: Text.RichText
                    text: '<a href="https://github.com/cupy/cupy/blob/main/LICENSE">CuPy</a> · 主项目 MIT License。'
                    onLinkActivated: function(link) { Qt.openUrlExternally(link) }
                }
                Label {
                    width: parent.width
                    wrapMode: Text.WordWrap
                    textFormat: Text.RichText
                    text: '<a href="https://github.com/pytorch/pytorch/blob/main/LICENSE">PyTorch</a> · 主项目 BSD 3-Clause；发行包还有其他组件许可。'
                    onLinkActivated: function(link) { Qt.openUrlExternally(link) }
                }
                Label {
                    width: parent.width
                    text: "torch-directml：实际安装包许可待核对，暂不标注为开源许可。"
                    wrapMode: Text.WordWrap
                }
            }
        }
    }

    Component {
        id: licensesPage
        ScrollView {
            id: licensesScroll
            clip: true
            ScrollBar.vertical.policy: ScrollBar.AsNeeded
            Column {
                width: Math.max(1, licensesScroll.availableWidth - 22)
                spacing: 11
                Label {
                    text: "开源协议"
                    font.pixelSize: 25
                    font.bold: true
                }
                Label {
                    width: parent.width
                    text: "本项目软件代码采用 MIT License。Copyright (c) 2026 ii33。完整文本随源码附在根目录 LICENSE。"
                    wrapMode: Text.WordWrap
                }
                Label {
                    width: parent.width
                    text: "软件按许可证原文提供，不附带适销性或特定用途保证。第三方代码、字体与材料数据需同时遵守各自的许可及来源要求。"
                    wrapMode: Text.WordWrap
                }
                Label {
                    width: parent.width
                    text: "第三方依赖各自的许可与本项目协议分别适用，详见“开源声明”页的官方出处。"
                    wrapMode: Text.WordWrap
                }
            }
        }
    }
}
