"""Read-only network diagnostics through an explicit adapter; no global mutations."""
from collections.abc import Mapping
import json
from urllib.parse import urlsplit
from .contracts import ToolSpec, ToolkitError
from .execution import STRING
from .environment import secure_url
from .policy import check_cancelled


def network_route(url, config):
    """CNB direct; GitHub proxy only when explicitly configured by the host."""
    secure_url(url)
    host = urlsplit(url).hostname.lower()
    if host == 'cnb.cool' or host.endswith('.cnb.cool'):
        return {'mode': 'direct'}
    proxy = config.get('github_proxy_url')
    if proxy and (host == 'github.com' or host.endswith('.github.com') or host == 'githubusercontent.com' or host.endswith('.githubusercontent.com')):
        parts = urlsplit(proxy)
        try:
            valid = parts.scheme in ('http', 'https', 'socks5', 'socks5h') and parts.hostname and parts.port and not parts.username and not parts.password and not parts.query and not parts.fragment and parts.path in ('', '/')
        except ValueError:
            valid = False
        if not valid:
            raise ToolkitError('invalid_configuration', 'Proxy must be an explicit credential-free proxy URL')
        return {'mode': 'proxy', 'proxy_url': proxy}
    return {'mode': 'direct'}


def net_check(context, args):
    provider = context.providers.get('network')
    if not callable(provider):
        raise ToolkitError('provider_unavailable', 'A sanctioned network diagnostics adapter is required')
    config = context.providers.get('network_config', {})
    if not isinstance(config, Mapping):
        raise ToolkitError('invalid_configuration', 'network_config must be a mapping')
    results = []
    for url in args['urls']:
        check_cancelled(context)
        request = {'url': secure_url(url), 'route': network_route(url, config), 'method': 'HEAD',
                   'timeout_ms': args.get('timeout_ms', 8000), 'verify_tls': True,
                   'follow_redirects': False, 'max_output_bytes': 4096}
        response = provider(request, context)
        check_cancelled(context)
        if not isinstance(response, Mapping) or type(response.get('reachable')) is not bool:
            raise ToolkitError('provider_contract', 'Network adapter must return reachable as a boolean')
        row = {'url': url, 'route': request['route'], 'reachable': response['reachable']}
        for key in ('status_code', 'latency_ms'):
            value = response.get(key)
            if value is not None:
                if type(value) not in (int, float) or value < 0:
                    raise ToolkitError('provider_contract', 'Network diagnostics contain invalid numbers')
                row[key] = value
        results.append(row)
    # A report only, never changes os.environ, proxy settings, TLS or host configuration.
    json.dumps(results, allow_nan=False)
    return {'checks': results, 'settings_changed': False}


def register_network_tools(registry):
    registry.register(ToolSpec('net_check', 'proxy', 'Read-only HTTPS connectivity diagnostics; configured routes only',
        {'type': 'object', 'properties': {'urls': {'type': 'array', 'items': STRING, 'minItems': 1, 'maxItems': 16},
        'timeout_ms': {'type': 'integer', 'minimum': 1, 'maximum': 30000}}, 'required': ['urls'], 'additionalProperties': False}), net_check)
