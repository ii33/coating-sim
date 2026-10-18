"""SFTP boundary with host aliases and strict host verification; no SSH process.

Trusted remote_fs adapters resolve credentials from external env/key references,
verify known hosts, constrain remote roots and enforce bounded transport/cancel.
The toolkit never reads credential values or returns host configuration/secrets.
"""
from collections.abc import Mapping
import json
import posixpath
import re
from .contracts import ToolSpec, ToolkitError
from .execution import STRING
from .policy import check_cancelled

ALIAS = re.compile(r'^[a-z][a-z0-9_-]{0,63}$')
ENV_NAME = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')
HOST_FIELDS = {'hostname', 'port', 'username', 'password_env', 'key_ref', 'known_hosts', 'strict_host_key', 'roots'}


def validate_host(alias, host):
    if not isinstance(alias, str) or not ALIAS.fullmatch(alias) or not isinstance(host, Mapping):
        raise ToolkitError('invalid_configuration', 'Invalid remote alias or host configuration')
    if set(host) - HOST_FIELDS or host.get('strict_host_key') is not True:
        raise ToolkitError('invalid_configuration', 'Strict known-host verification is mandatory; unsupported host fields are forbidden')
    for key in ('hostname', 'known_hosts'):
        if not isinstance(host.get(key), str) or not host[key] or '\x00' in host[key]:
            raise ToolkitError('invalid_configuration', 'Hostname and known-hosts reference are required')
    if type(host.get('port', 22)) is not int or not 1 <= host.get('port', 22) <= 65535:
        raise ToolkitError('invalid_configuration', 'Invalid SSH port')
    if 'password_env' in host and (not isinstance(host['password_env'], str) or not ENV_NAME.fullmatch(host['password_env'])):
        raise ToolkitError('invalid_configuration', 'Passwords may only be referenced by environment variable name')
    if 'key_ref' in host and (not isinstance(host['key_ref'], str) or not host['key_ref'] or '\n' in host['key_ref'] or '\x00' in host['key_ref'] or 'PRIVATE KEY' in host['key_ref']):
        raise ToolkitError('invalid_configuration', 'Keys must be external references, never key material')
    roots = host.get('roots', ())
    if not isinstance(roots, (list, tuple)) or not roots or any(not isinstance(root, str) or not root.startswith('/') or '\x00' in root or '\\' in root or '..' in root.split('/') for root in roots):
        raise ToolkitError('invalid_configuration', 'Explicit absolute remote roots are required')
    return host


def remote_hosts(context):
    hosts = context.providers.get('remote_hosts', {})
    if not isinstance(hosts, Mapping):
        raise ToolkitError('invalid_configuration', 'remote_hosts must be a mapping')
    return hosts


def checked_remote_call(context, operation, args):
    check_cancelled(context)
    alias = args.get('host', '').lower()
    hosts = remote_hosts(context)
    if alias not in hosts:
        raise ToolkitError('unknown_host', 'Remote alias is not configured')
    host = validate_host(alias, hosts[alias])
    path = args.get('path', '/')
    if not isinstance(path, str) or not path.startswith('/') or '\x00' in path or '\\' in path or '..' in path.split('/'):
        raise ToolkitError('path_denied', 'Invalid remote path')
    path = posixpath.normpath(path)
    if operation != 'ping' and not any(path == posixpath.normpath(root) or path.startswith(posixpath.normpath(root).rstrip('/') + '/') for root in host['roots']):
        raise ToolkitError('path_denied', 'Remote path lies outside configured roots')
    provider = context.providers.get('remote_fs')
    if not callable(provider):
        raise ToolkitError('provider_unavailable', 'A sanctioned SFTP adapter is required')
    request = {**args, 'host': alias, 'path': path, 'strict_host_key': True,
               'timeout_ms': min(args.get('timeout_ms', 30000), 600000),
               'max_output_bytes': min(args.get('max_output_bytes', 1024 * 1024), 1024 * 1024)}
    result = provider(operation, request, context)
    check_cancelled(context)
    if not isinstance(result, Mapping):
        raise ToolkitError('provider_contract', 'Remote adapter must return an object')
    if len(json.dumps(result, allow_nan=False).encode()) > request['max_output_bytes']:
        raise ToolkitError('limit_exceeded', 'Remote response exceeded output budget')
    return result


def sftp_hosts(context, args):
    action = args.get('action', 'list')
    if action == 'list':
        rows = []
        for alias, host in remote_hosts(context).items():
            validate_host(alias, host)
            rows.append({'alias': alias, 'configured': True, 'transport_ready': callable(context.providers.get('remote_fs'))})
        return {'hosts': sorted(rows, key=lambda row: row['alias'])}
    return checked_remote_call(context, 'ping', {'host': args.get('host', ''), 'path': '/', 'timeout_ms': args.get('timeout_ms', 30000)})


def search_remote(context, args):
    # Provider must implement bounded literal search. No unbounded regex backend.
    return checked_remote_call(context, 'search_remote', {**args, 'literal': True, 'max_results': args.get('max_results', 100), 'max_files': args.get('max_files', 1000), 'max_bytes': args.get('max_bytes', 1024 * 1024)})


def register_remote_tools(registry):
    definitions = [
        ('sftp_hosts', sftp_hosts, {'action': {'type': 'string', 'enum': ['list', 'ping']}, 'host': STRING,
         'timeout_ms': {'type': 'integer', 'minimum': 1, 'maximum': 600000}}, []),
        ('search_remote', search_remote, {'host': STRING, 'path': STRING, 'query': STRING,
         'max_results': {'type': 'integer', 'minimum': 1, 'maximum': 1000},
         'max_files': {'type': 'integer', 'minimum': 1, 'maximum': 10000},
         'max_bytes': {'type': 'integer', 'minimum': 1, 'maximum': 1024 * 1024},
         'timeout_ms': {'type': 'integer', 'minimum': 1, 'maximum': 600000}}, ['host', 'path', 'query'])]
    for name, handler, properties, required in definitions:
        registry.register(ToolSpec(name, 'sftp', 'Strictly verified SFTP adapter operation',
            {'type': 'object', 'properties': properties, 'required': required, 'additionalProperties': False}), handler)
