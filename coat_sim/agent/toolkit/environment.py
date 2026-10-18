"""Probe the selected interpreter, never PATH pip; installation is explicit."""
import json
import re
from urllib.parse import urlsplit
from .contracts import ToolSpec, ToolkitError
from .execution import execute, selected_executable, STRING, TIMEOUT

PROBE = "import sys,json,importlib.util; print(json.dumps({'executable':sys.executable,'version':sys.version,'pip':importlib.util.find_spec('pip') is not None}))"
PACKAGE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]*(?:\[[A-Za-z0-9_,.-]+\])?(?:(?:==|>=|<=|~=|!=|>|<)[A-Za-z0-9.*+!_-]+)?$')


def secure_url(value):
    if any(ord(char) < 33 for char in value):
        raise ToolkitError('invalid_arguments', 'URL must not contain whitespace or control characters')
    parts = urlsplit(value)
    if parts.scheme != 'https' or not parts.hostname or parts.username or parts.password or parts.fragment:
        raise ToolkitError('invalid_arguments', 'A credential-free HTTPS URL is required')
    return value


def probe_python(context, args):
    selected = selected_executable(context, 'python')
    result = execute(context, [selected, '-c', PROBE], args)
    if result['exit_code'] != 0 or result['truncated']:
        raise ToolkitError('dependency_unavailable', 'Selected Python probe failed')
    try:
        metadata = json.loads(result['stdout'])
        if not isinstance(metadata['executable'], str) or not metadata['executable'] or type(metadata['pip']) is not bool:
            raise ValueError()
    except (ValueError, KeyError, TypeError):
        raise ToolkitError('provider_contract', 'Selected Python probe returned invalid metadata') from None
    return selected, metadata


def env_setup(context, args):
    selected, metadata = probe_python(context, args)
    return {'selected_python': selected, 'python': metadata, 'installation_performed': False,
            'readiness': {'execution': True, 'pip': metadata['pip']},
            'pip_command': [selected, '-m', 'pip']}


def pip_argv(python, packages, index_url=None):
    if not packages or any(not PACKAGE.fullmatch(item) for item in packages):
        raise ToolkitError('invalid_arguments', 'Only named package requirements are supported; no flags, URLs or local paths')
    argv = [python, '-m', 'pip', 'install', '--disable-pip-version-check', '--no-input']
    if index_url:
        argv += ['--index-url', secure_url(index_url)]
    return argv + list(packages)


def pip_install(context, args):
    # Registry authorization covers this install-specific tool and exact arguments.
    selected = selected_executable(context, 'python')
    argv = pip_argv(selected, args['packages'], args.get('index_url'))
    _, metadata = probe_python(context, args)
    if not metadata['pip']:
        raise ToolkitError('dependency_unavailable', 'Selected interpreter has no pip; no automatic bootstrap is performed')
    result = execute(context, argv, args)
    result.update({'selected_python': selected, 'actual_python': metadata['executable']})
    if result['exit_code'] == 0:
        names = [re.split(r'[\[<>=!~]', item)[0] for item in args['packages']]
        verification = execute(context, [selected, '-m', 'pip', 'show', *names], args)
        result['verification'] = verification
        result['verified'] = verification['exit_code'] == 0
    return result


def register_environment_tools(registry):
    common = {'cwd': STRING, 'timeout_ms': TIMEOUT}
    for name, handler, extra, required, effect in [
        ('env_setup', env_setup, {}, [], 'execute'),
        ('pip_install', pip_install, {'packages': {'type': 'array', 'items': STRING, 'minItems': 1, 'maxItems': 64}, 'index_url': STRING}, ['packages'], 'install')]:
        registry.register(ToolSpec(name, 'env', 'Probe or explicitly install into the selected Python environment', {'type': 'object', 'properties': {**common, **extra}, 'required': required, 'additionalProperties': False}, effect), handler)
