"""Sanctioned execution boundary. This module never starts a process itself.

An injected execution(request, context) adapter MUST enforce the application's
sandbox and approvals, kill/reap on cancellation/timeout, and drain bounded
output. String guards below are hygiene, not a sandbox or command allowlist.
"""
from collections.abc import Mapping
import re
from pathlib import PureWindowsPath
from .contracts import ToolSpec, ToolkitError
from .policy import check_cancelled, resolve_path

MAX_OUTPUT = 1024 * 1024
TIMEOUT = {'type': 'integer', 'minimum': 1, 'maximum': 600000}
STRING = {'type': 'string', 'minLength': 1, 'maxLength': 65536}
BANNED = {'cmd', 'powershell', 'powershell_ise'}


def executable_name(value):
    return PureWindowsPath(value.replace('/', '\\')).name.lower().removesuffix('.exe')


def validate_argv(argv):
    if not isinstance(argv, (tuple, list)) or not argv or len(argv) > 256:
        raise ToolkitError('invalid_arguments', 'Expected 1–256 argv items')
    if any(not isinstance(item, str) or '\x00' in item for item in argv) or not argv[0]:
        raise ToolkitError('invalid_arguments', 'argv must contain NUL-free strings and an executable')
    if executable_name(argv[0]) in BANNED:
        raise ToolkitError('shell_denied', 'cmd and legacy Windows PowerShell are disabled')
    return list(argv)


def guard_shell(command):
    if '\x00' in command:
        raise ToolkitError('invalid_arguments', 'NUL is not permitted')
    # Conservative token scan also catches nested/remote shell invocations.
    # This can reject literal mentions: it never purports to parse shell syntax.
    tokens = re.findall(r'[A-Za-z0-9_./\\:-]+', command)
    if any(executable_name(token) in BANNED for token in tokens):
        raise ToolkitError('shell_denied', 'cmd and legacy Windows PowerShell are disabled')


def selected_executable(context, name):
    config = context.providers.get('execution_config', {})
    value = config.get(name) if isinstance(config, Mapping) else None
    if not isinstance(value, str) or not value or '\x00' in value:
        raise ToolkitError('dependency_unavailable', f'No selected {name} interpreter configured')
    validate_argv([value])
    return value


def execute(context, argv, args):
    check_cancelled(context)
    provider = context.providers.get('execution')
    if not callable(provider):
        raise ToolkitError('provider_unavailable', 'A sanctioned execution adapter is required')
    request = {'argv': validate_argv(argv), 'cwd': str(resolve_path(context, args.get('cwd', '.'))),
               'timeout_ms': args.get('timeout_ms', 120000),
               'max_output_bytes': args.get('max_output_bytes', MAX_OUTPUT),
               'shell': False, 'env': {'PYTHONUTF8': '1', 'PYTHONIOENCODING': 'utf-8'}}
    result = provider(request, context)
    check_cancelled(context)
    if not isinstance(result, Mapping) or type(result.get('exit_code')) is not int:
        raise ToolkitError('provider_contract', 'Execution adapter must return an integer exit_code')
    data = {'exit_code': result['exit_code'], 'truncated': bool(result.get('truncated', False))}
    budget = request['max_output_bytes']
    for key in ('stdout', 'stderr'):
        value = result.get(key, '')
        if not isinstance(value, str):
            raise ToolkitError('provider_contract', 'Execution output must be text')
        raw = value.encode('utf-8')
        data[key] = raw[:budget].decode('utf-8', errors='ignore')
        data['truncated'] |= len(raw) > budget
        budget = max(0, budget - len(raw))
    if result.get('timed_out'):
        raise ToolkitError('timeout', 'Execution adapter reported a timeout')
    return data


def run(context, args):
    return execute(context, args['argv'], args)


def sh(context, args):
    guard_shell(args['command'])
    return execute(context, [selected_executable(context, 'bash'), '--noprofile', '--norc', '-c', args['command']], args)


def pwsh(context, args):
    guard_shell(args['command'])
    executable = selected_executable(context, 'pwsh')
    version = execute(context, [executable, '-NoLogo', '-NoProfile', '-NonInteractive', '-Command', '$PSVersionTable.PSVersion.Major'], args)
    if version['exit_code'] != 0 or not re.fullmatch(r'\s*(?:[7-9]|[1-9][0-9]+)\s*', version['stdout']):
        raise ToolkitError('dependency_unavailable', 'Selected PowerShell must probe as version 7 or newer')
    prefix = '[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new(); $OutputEncoding = [Console]::OutputEncoding; '
    return execute(context, [executable, '-NoLogo', '-NoProfile', '-NonInteractive', '-Command', prefix + args['command']], args)


def register_execution_tools(registry):
    common = {'cwd': STRING, 'timeout_ms': TIMEOUT, 'max_output_bytes': {'type': 'integer', 'minimum': 1, 'maximum': MAX_OUTPUT}}
    for name, handler in [('run', run), ('sh', sh), ('pwsh', pwsh)]:
        field = 'argv' if name == 'run' else 'command'
        schema = {'type': 'array', 'items': {'type': 'string', 'maxLength': 65536}, 'minItems': 1, 'maxItems': 256} if name == 'run' else STRING
        registry.register(ToolSpec(name, name, 'Execute through a sanctioned, bounded adapter', {'type': 'object', 'properties': {**common, field: schema}, 'required': [field], 'additionalProperties': False}, 'execute'), handler)
