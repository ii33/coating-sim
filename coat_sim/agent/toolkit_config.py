"""Backend-neutral, credential-free toolkit settings; no registration or I/O on import."""
import os
import stat
from pathlib import Path

CAPABILITIES = ('fs', 'sftp', 'run', 'sh', 'pwsh', 'env', 'proxy', 'doc', 'split', 'img', 'tasks', 'guard', 'preset', 'memory')
PRIVATE_CAPABILITIES = ('fs', 'doc', 'split', 'img', 'tasks', 'preset', 'memory')
CAPABILITY_LABELS = {
    'fs': '本地文件', 'sftp': 'SFTP（需宿主提供连接）',
    'run': '程序执行（桌面适配器未开放）', 'sh': 'Shell（需 bash；桌面未开放）',
    'pwsh': 'PowerShell（需 7+；桌面未开放）',
    'env': '环境与安装（安装需授权；桌面未开放）',
    'proxy': '网络与代理（需网络提供器；桌面未开放）',
    'doc': '文档（OCR 需 docling 提供器）', 'split': '文件拆分',
    'img': '图像（需 Pillow，可选依赖）', 'tasks': '任务账本',
    'guard': '安全守卫（始终生效；勾选仅为能力元数据）', 'preset': '可迁移配置（JSON 数据）',
    'memory': '长期记忆（仅保存精简重要信息）',
}


def default_workspace_root():
    """Stable per-user data scope, never CWD, install, project or credentials.

    This function computes a path only. The host creates it lazily under live
    operational authority; settings rendering and discussion stay read-only.
    """
    if os.name == 'nt':
        fallback = Path.home() / 'AppData' / 'Local'
        configured = os.environ.get('LOCALAPPDATA', '')
    else:
        fallback = Path.home() / '.local' / 'share'
        configured = os.environ.get('XDG_DATA_HOME', '')
    base = Path(configured) if configured and '\x00' not in configured else fallback
    if not base.is_absolute():
        base = fallback
    return base / 'CoatSim' / 'assistant-workspace'


def is_private_workspace_root(value):
    return isinstance(value, str) and value.strip() == str(default_workspace_root())


def toolkit_setting_defaults(source):
    """Missing settings opt into our own workspace, never an arbitrary root.

    Present false/empty values are not missing and are preserved by callers.
    Explicit custom roots keep the previous fully opt-in behavior.
    """
    root = source.get('assistant_toolkit_workspace_root', str(default_workspace_root()))
    private = is_private_workspace_root(root)
    return {
        'assistant_toolkit_enabled': private,
        'assistant_toolkit_workspace_root': root,
        'assistant_toolkit_capabilities': list(PRIVATE_CAPABILITIES) if private else [],
        'assistant_toolkit_profile_capabilities': list(PRIVATE_CAPABILITIES) if private else [],
        'assistant_toolkit_allow_model_context': private,
    }


def normalized_capabilities(value):
    if not isinstance(value, (list, tuple)):
        return []
    return [name for name in CAPABILITIES if name in value]


def normalized_toolkit_settings(source):
    source = {**toolkit_setting_defaults(source), **source}
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


def validate_workspace_root(value, installation_root=None, *, allow_missing=False):
    if not isinstance(value, str) or not value.strip() or '\x00' in value or len(value) > 4096:
        raise ValueError('请明确填写工具集工作区的绝对目录')
    path = Path(value.strip())
    if not path.is_absolute():
        raise ValueError('工具集工作区必须使用绝对路径')
    try:
        for part in (path, *path.parents):
            try:
                info = part.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                raise ValueError('工具集工作区不可使用符号链接或重解析点')
        path = path.resolve(strict=not allow_missing)
    except (OSError, RuntimeError) as exc:
        raise ValueError('工具集工作区不存在或不可访问') from exc
    if (path.exists() and not path.is_dir()) or (not allow_missing and not path.is_dir()) or path == Path(path.anchor):
        raise ValueError('工具集工作区必须是现有的非磁盘根目录')
    install = Path(installation_root or Path(__file__).resolve().parents[2]).resolve()
    if path == install or path in install.parents or install in path.parents:
        raise ValueError('工具集工作区须独立于软件安装目录及其父目录')
    return path
