"""Explicit local roots. Remote paths must never silently become local paths."""
from contextlib import contextmanager
import os
import re
import stat
from pathlib import Path, PureWindowsPath
from .contracts import ToolkitError, _freeze

REMOTE_PATH = re.compile(r'^([A-Za-z][A-Za-z0-9_-]*):/(.*)$')


def check_cancelled(context):
    if context.cancelled():
        raise ToolkitError('cancelled', 'Operation cancelled')


def check_authorized(context, spec, args):
    """Recheck the live host grant immediately before a filesystem commit."""
    check_cancelled(context)
    if (spec.capability not in context.allowed_capabilities
            or context.authorize(spec, _freeze(args)) is not True):
        raise ToolkitError('authorization_denied', 'Operation is no longer authorized')
    check_cancelled(context)


def remote_path(path):
    if os.name == 'nt' and re.match(r'^[A-Za-z]:[\\/]', str(path)):
        return None  # Native drive paths are never SFTP aliases.
    match = REMOTE_PATH.match(str(path))
    return (match.group(1).lower(), '/' + match.group(2)) if match else None


def validate_path_syntax(value: str, *, windows: bool) -> None:
    if '\x00' in value or (not windows and '\\' in value):
        raise ToolkitError('path_denied', 'NUL or non-native path is not permitted')
    if windows:
        native = PureWindowsPath(value)
        components = native.parts[1:] if native.anchor else native.parts
        if (value.startswith(('\\\\', '//')) or '..' in native.parts
                or (native.drive and not native.is_absolute())
                # Reject alternate streams, trim aliases and DOS short-name
                # aliases rather than letting them bypass reserved state paths.
                or any(':' in part or '~' in part or part.endswith((' ', '.'))
                       for part in components)
                or native.is_reserved()):
            raise ToolkitError('path_denied', 'UNC, device, alternate/short-name aliases, traversal or drive-relative paths are not permitted')


def resolve_path(context, path):
    check_cancelled(context)
    if remote_path(path):
        raise ToolkitError('remote_provider_required', 'Remote paths require an explicit provider')
    value = Path(path)
    validate_path_syntax(str(path), windows=os.name == 'nt')
    if '..' in value.parts:
        raise ToolkitError('path_denied', 'Traversal is not permitted')
    if not context.allowed_roots:
        raise ToolkitError('path_denied', 'No permitted filesystem roots')
    if not value.is_absolute():
        value = context.allowed_roots[0] / value
    value = Path(os.path.abspath(value))
    if not any(value.is_relative_to(root) for root in context.allowed_roots):
        raise ToolkitError('path_denied', 'Path lies outside permitted roots')
    for part in (value, *value.parents):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ToolkitError('path_denied', 'Symlinks and reparse points are not permitted')
    return value


def user_path(context, path):
    """Generic file mutations cannot replace toolkit control or recovery state."""
    value = resolve_path(context, path)
    root = max((root for root in context.allowed_roots if value.is_relative_to(root)),
               key=lambda item: len(item.parts))
    if any(part.casefold() == '.toolkit' or part.casefold().startswith('.toolkit-')
           for part in value.relative_to(root).parts):
        raise ToolkitError('path_denied', 'Toolkit control and recovery state is reserved')
    return value


def user_write_path(context, path):
    """Public generic tools cannot bypass managed project revision/history APIs."""
    value = user_path(context, path)
    from coat_sim.managed_paths import assert_not_managed_project_path
    try:
        assert_not_managed_project_path(value)
    except (OSError, ValueError) as exc:
        raise ToolkitError('path_denied', 'Managed project files require their project service; generic writes are denied') from exc
    return value


@contextmanager
def pinned_parent(context, path):
    """Pin every parent without following links on dir-fd platforms.

    Other platforms retain explicit reparse checks; this is not an OS sandbox.
    """
    target = resolve_path(context, path)
    if os.open not in os.supports_dir_fd or not hasattr(os, 'O_NOFOLLOW'):
        yield None, target
        resolve_path(context, path)
        return
    fd = os.open(target.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for component in target.parent.parts[1:]:
            next_fd = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        yield fd, target
    finally:
        os.close(fd)
