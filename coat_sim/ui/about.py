"""Read-only resources for the independent About and Help window."""

from pathlib import Path

from PySide6.QtCore import QObject, Property, QUrl, Slot

from coat_sim.version import BUILD_IDENTITY, format_git_commit_label


ROOT = Path(__file__).resolve().parents[2]


class AboutBridge(QObject):
    @Property(str, constant=True)
    def internalVersion(self) -> str:
        return BUILD_IDENTITY.version

    @Property(str, constant=True)
    def gitCommitLabel(self) -> str:
        return format_git_commit_label(BUILD_IDENTITY)

    @Property(str, constant=True)
    def openSourceHtml(self) -> str:
        return (
            '<p><b>看板娘运行依赖：</b></p>'
            '<p><a href="https://github.com/pydantic/pydantic-ai/blob/main/LICENSE">'
            'Pydantic AI Slim 2.51.0</a> · MIT License；模型和工具执行接口。</p>'
            '<p><a href="https://github.com/pydantic/pydantic-ai-harness/blob/v0.36.0/LICENSE">'
            'Pydantic AI Harness 0.36.0</a> · MIT License；规划、技能、上下文管理与步骤持久化。</p>'
            '<p><a href="https://github.com/jlowin/fastmcp/blob/v4.0.10/LICENSE">'
            'FastMCP Slim 4.0.10</a> · Apache License 2.0；连接本项目 MCP 服务的客户端。</p>'
            '<p><b>CAD与系统凭据依赖：</b></p>'
            '<p><a href="https://github.com/mozman/ezdxf/blob/master/LICENSE">'
            'ezdxf 1.4.4 及兼容 1.x</a> · MIT License；二维修正板 DXF 交换。</p>'
            '<p><a href="https://github.com/jaraco/keyring/blob/main/pyproject.toml">'
            'keyring 25.x</a> · MIT License；系统安全凭据接口。'
            '各平台系统后端仍需可用的原生凭据服务，不使用明文文件回退。</p>'
            '<p><b>沙箱图表依赖：</b></p>'
            '<p><a href="https://github.com/Kozea/pygal">'
            'Pygal 3.0.5</a> · GNU LGPL v3 或更新版本；离线 SVG 图表。'
            '原许可证保留在私有环境的 pygal-3.0.5.dist-info/COPYING 中。</p>'
            '<p><a href="https://github.com/python/importlib_metadata">importlib-metadata 9.0.1</a>'
            ' · Apache License 2.0；<a href="https://github.com/jaraco/zipp">zipp 4.1.0</a>'
            ' · MIT License；图表库依赖。</p>'
            '<p><a href="https://docs.python.org/3/license.html">CPython</a> · PSF License；私有 Python 环境。</p>'
            '<p><b>随包界面字体：</b><a href="https://github.com/notofonts/noto-cjk">'
            'Noto Sans CJK SC</a> · SIL Open Font License 1.1。'
            '原始字体、版权与完整许可证保存在 ui/assets/fonts/noto-cjk；应用内加载，无须在系统安装。</p>'
        )

    @Property(QUrl, constant=True)
    def boardUrl(self) -> QUrl:
        return QUrl.fromLocalFile(str(Path(__file__).resolve().parent / "assets" / "board.png"))

    @Property(QUrl, constant=True)
    def algorithmDocUrl(self) -> QUrl:
        return QUrl.fromLocalFile(str(ROOT / "docs" / "algorithms.html"))

    @Property(QUrl, constant=True)
    def softwareManualUrl(self) -> QUrl:
        return QUrl.fromLocalFile(str(ROOT / "docs" / "software_manual.html"))

    @Slot(str, result=QUrl)
    def manualSectionUrl(self, section: str) -> QUrl:
        allowed = {"overview", "workflow", "coordinates", "source", "substrate",
                   "machine", "process", "mask", "cad", "results", "assistant", "limitations",
                   "projects", "compute_devices", "film_design", "algorithm_selection",
                   "film_optimization", "film_optimization_parameters"}
        url = self.softwareManualUrl
        if section in allowed:
            url.setFragment(section)
        return url
