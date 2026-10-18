"""Backend-neutral, credential-free toolkit settings; no registration or I/O on import."""
from pathlib import Path

CAPABILITIES = ('fs', 'sftp', 'run', 'sh', 'pwsh', 'env', 'proxy', 'doc', 'split', 'img', 'tasks', 'guard', 'preset')
CAPABILITY_LABELS = {
    'fs': '本地文件', 'sftp': 'SFTP（需宿主提供连接）',
    'run': '程序执行（桌面适配器未开放）', 'sh': 'Shell（需 bash；桌面未开放）',
    'pwsh': 'PowerShell（需 7+；桌面未开放）',
    'env': '环境与安装（安装需授权；桌面未开放）',
    'proxy': '网络与代理（需网络提供器；桌面未开放）',
    'doc': '文档（OCR 需 docling 提供器）', 'split': '文件拆分',
    'img': '图像（需 Pillow，可选依赖）', 'tasks': '任务账本',
    'guard': '安全守卫（始终生效；勾选仅为能力元数据）', 'preset': '可迁移配置（JSON 数据）',
}


def normalized_capabilities(value):
    if not isinstance(value, (list, tuple)):
        return []
    return [name for name in CAPABILITIES if name in value]


def normalized_toolkit_settings(source):
    root = source.get('assistant_toolkit_workspace_root', '')
    root = root.strip() if isinstance(root, str) else ''
    if len(root) > 4096 or '\x00' in root:
        root = ''
    return {
        'assistant_toolkit_enabled': source.get('assistant_toolkit_enabled') is True,
        'assistant_toolkit_workspace_root': root,
        'assistant_toolkit_capabilities': normalized_capabilities(source.get('assistant_toolkit_capabilities')),
        'assistant_toolkit_profile_capabilities': normalized_capabilities(source.get('assistant_toolkit_profile_capabilities')),
        'assistant_toolkit_allow_model_context': source.get('assistant_toolkit_allow_model_context') is True,
    }


def validate_workspace_root(value, installation_root=None):
    if not isinstance(value, str) or not value.strip() or '\x00' in value or len(value) > 4096:
        raise ValueError('请明确填写工具集工作区的绝对目录')
    path = Path(value.strip())
    if not path.is_absolute():
        raise ValueError('工具集工作区必须使用绝对路径')
    try:
        path = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValueError('工具集工作区不存在或不可访问') from exc
    if not path.is_dir() or path == Path(path.anchor):
        raise ValueError('工具集工作区必须是现有的非磁盘根目录')
    install = Path(installation_root or Path(__file__).resolve().parents[2]).resolve()
    if path == install or path in install.parents or install in path.parents:
        raise ValueError('工具集工作区须独立于软件安装目录及其父目录')
    return path
