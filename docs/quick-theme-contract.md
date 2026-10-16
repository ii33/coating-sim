# Qt Quick 主题接口契约（初版）

本文说明当前 Qt Quick 主题服务、持久化设置及各 QML engine 的接入方式。启动器、机器/掩模编辑器、源/基片独立窗口、计算结果查看器，以及膜系旧界面的 QQuickWidget 和材料库 engine 均通过同一个进程级 ThemeService 获得只读桥接。

同一进程的 engine 通过 ThemeService 信号即时同步。源/基片隔离子进程各自读取共享 ui_settings.json 中的已保存主题偏好；项目没有进程间即时主题通知机制，已运行的其他进程需要重新启动后读取新偏好。默认偏好为 system；新建或缺少字号字段的设置默认使用 9 pt，旧设置文件中显式保存的字号和 DPI 保持不变。


## 接入 API

```python
from coat_sim.ui.theme_service import ThemeService

theme_service = ThemeService.instance()  # 进程内唯一写源
bridge = theme_service.attach_engine(engine, "Theme")

# 用户设置变化时，以一次完整快照通知所有已接入的 engine。
theme_service.set_theme("dark")
theme_service.set_accessibility(font_scale=1.2, density_scale=1.1)
```

ThemeEngineBridge 为每个 QML engine 提供只读的 `snapshot`、`revision`、`mode`、`preferenceMode`、`isDark`、`themeId`、`themeName` 和 `assets`。

```qml
Rectangle {
    color: Theme.snapshot.colorSurface
    border.color: Theme.snapshot.colorBorder
}

Rectangle {
    color: Theme.snapshot.colorScrim
    opacity: Theme.snapshot.opacityScrim
}
```

Python 侧每次有效配置变更都会整体替换 49 项快照、递增 `revision` 一次，并发出变更通知。快照属性没有 QML setter，读取时返回副本；业务页面不得直接修改它。服务应在其所属线程（应用中通常是 GUI 线程）更新。

## 49 个语义 token

颜色以不含透明度的 `#RRGGBB` 表示；透明度单独使用 0 到 1 的 opacity token。

| 分类 | token |
|---|---|
| 颜色（20） | `colorWindow`, `colorSurface`, `colorSurfaceRaised`, `colorText`, `colorTextMuted`, `colorTextOnAccent`, `colorBorder`, `colorBorderStrong`, `colorAccent`, `colorAccentHover`, `colorAccentPressed`, `colorFocusRing`, `colorSelection`, `colorSuccess`, `colorWarning`, `colorError`, `colorInfo`, `colorDisabledText`, `colorDisabledSurface`, `colorScrim` |
| 透明度（4） | `opacityDisabled`, `opacityHover`, `opacityPressed`, `opacityScrim` |
| 字体与排版（8） | `fontFamily`, `fontSizeSmall`, `fontSizeBody`, `fontSizeHeading`, `fontSizeTitle`, `fontWeightNormal`, `fontWeightEmphasis`, `lineHeightBody` |
| 间距（5） | `spaceXs`, `spaceSm`, `spaceMd`, `spaceLg`, `spaceXl` |
| 控件高度（3） | `controlHeightSm`, `controlHeightMd`, `controlHeightLg` |
| 圆角（3） | `radiusSm`, `radiusMd`, `radiusLg` |
| 边框（2） | `borderWidth`, `focusRingWidth` |
| 动效（2） | `durationFast`, `durationNormal` |
| 可访问性缩放（2） | `fontScale`, `densityScale` |

当前支持 `system`（跟随启动时/运行时可用的系统调色板）、`light`、`dark`、`high-contrast-light`、`high-contrast-dark` 五种偏好。`Theme.mode` 是当前解析后的配色，`Theme.preferenceMode` 是用户选项，`Theme.isDark` 供 QML 判断深色。高对比配色是本项目提供的默认值，不构成 WCAG 合规认证。`fontSize*` 以逻辑 pt 表示，fontScale 从设置页 8–32 pt 字号映射；densityScale 从 75–150% 控件间距设置映射。

## DPI、图表和数据颜色

Qt Quick 和操作系统负责每个屏幕的 device-pixel scaling。QML 不应再把 `Screen.devicePixelRatio` 乘到本契约的逻辑像素上，以免重复缩放。`fontScale` 和 `densityScale` 是用户可访问性/紧凑度设置，不代替 Qt 的屏幕 DPI 处理。不同窗口移到不同 DPI 屏幕后仍由各自 Qt 窗口按系统逻辑坐标布局。

主题 token 只管通用界面及状态色。科学图表 colormap、曲线颜色、材料 `n/k` 数据映射必须继续由图表或数据可视化配置独立管理；切换明暗/高对比主题不能改变数据系列含义、计算值或导出值。

## 待核项目

- 本仓库未发现此前已有的完整主题 token 清单或 `ThemeService` 实现。本文件定义的是 49 项最小初版契约，若现有集成持有旧 token 名称/顺序/数值约定，应在接入前逐项映射，而不能声称本清单与不可见契约完全兼容。
- 当前默认数值和高对比调色板是初始产品约定，尚未完成多屏 DPI、中文字体和可访问性视觉验收。
- 现有启动器、设置页、助手和旧 QML 尚未改动；后续需在共享入口接入服务，再分窗口替换硬编码颜色。

## 主题包服务接入补充

设置页现在可导入本地主题 ZIP 并选择已安装主题；所选 ID 存于 `theme_package_id`。全局 `theme_mode` 决定所选包使用的 light/dark 变体，`system` 根据系统 palette 选取；high-contrast light/dark 使用对应明暗包变体，再应用包的 token 覆盖。

`ThemeEngineBridge` 为每个 QML engine 提供只读 `snapshot`、`revision`、`mode`、`preferenceMode`、`isDark`、`themeId`、`themeName` 与 `assets`。共享 `AppWindow` 消费背景资源，应用图标/悬浮看板娘/提示音由现有 Qt 与助手通知消费端热更新；缺失资源回退内置默认。主题色只映射至界面语义 token，不改科学图表配色。
