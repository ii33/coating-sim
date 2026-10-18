"""Strict validation, explicit capability intersection and per-call authorization."""
import json
import math
from collections.abc import Mapping
from .contracts import ToolSpec, ToolContext, ToolkitError, plain, _freeze

MAX_JSON_BYTES = 4 * 1024 * 1024


def validate(value, schema, path='$', depth=0):
    if depth > 32:
        raise ToolkitError('invalid_arguments', 'Maximum nesting exceeded')
    kind = schema.get('type')
    valid = {'object': isinstance(value, Mapping), 'array': isinstance(value, (list, tuple)),
             'string': isinstance(value, str), 'integer': type(value) is int,
             'number': type(value) in (int, float), 'boolean': type(value) is bool,
             'null': value is None}
    if kind is not None and not valid.get(kind, False):
        raise ToolkitError('invalid_arguments', f'{path}: expected {kind}')
    if type(value) is float and not math.isfinite(value):
        raise ToolkitError('invalid_arguments', f'{path}: finite number required')
    if 'enum' in schema and value not in schema['enum']:
        raise ToolkitError('invalid_arguments', f'{path}: unsupported value')
    if kind == 'object':
        properties = schema.get('properties', {})
        if set(value) - set(properties):
            raise ToolkitError('invalid_arguments', f'{path}: unknown fields')
        if set(schema.get('required', ())) - set(value):
            raise ToolkitError('invalid_arguments', f'{path}: missing required fields')
        for key, item in value.items():
            validate(item, properties[key], f'{path}.{key}', depth+1)
    if kind == 'array':
        if not schema.get('minItems', 0) <= len(value) <= schema.get('maxItems', 10000):
            raise ToolkitError('invalid_arguments', f'{path}: array size outside bounds')
        for item in value:
            validate(item, schema.get('items', {}), path+'[]', depth+1)
    if kind == 'string' and not schema.get('minLength', 0) <= len(value) <= schema.get('maxLength', MAX_JSON_BYTES):
        raise ToolkitError('invalid_arguments', f'{path}: string size outside bounds')
    if kind in ('integer', 'number') and not schema.get('minimum', -float('inf')) <= value <= schema.get('maximum', float('inf')):
        raise ToolkitError('invalid_arguments', f'{path}: number outside bounds')


class ToolRegistry:
    def __init__(self):
        self._tools = {}

    def register(self, spec: ToolSpec, handler):
        if spec.name in self._tools:
            raise ValueError(f'Duplicate tool: {spec.name}')
        if spec.parameters.get('type') != 'object':
            raise ValueError('Tool arguments must be an object schema')
        self._tools[spec.name] = (spec, handler)

    def specs(self, context: ToolContext | None = None):
        return tuple(spec for spec, _ in self._tools.values()
                     if context is None or spec.capability in context.allowed_capabilities)

    def invoke(self, name, args, context: ToolContext):
        try:
            if name not in self._tools:
                raise ToolkitError('unknown_tool', 'Unknown tool')
            spec, handler = self._tools[name]
            if spec.capability not in context.allowed_capabilities:
                raise ToolkitError('capability_disabled', 'Capability is not enabled')
            from .policy import check_cancelled
            check_cancelled(context)
            validate(args, spec.parameters)
            args = json.loads(json.dumps(plain(args), allow_nan=False))
            if len(json.dumps(args).encode()) > MAX_JSON_BYTES:
                raise ToolkitError('limit_exceeded', 'Input exceeds JSON byte budget')
            # Exact True only: truthy strings and async callbacks cannot authorize.
            if context.authorize(spec, _freeze(args)) is not True:
                raise ToolkitError('authorization_denied', 'Operation was not authorized')
            check_cancelled(context)
            data = plain(handler(context, args))
            encoded = json.dumps(data, allow_nan=False)
            if len(encoded.encode()) > MAX_JSON_BYTES:
                raise ToolkitError('limit_exceeded', 'Result exceeds JSON byte budget')
            return {'ok': True, 'data': data, 'error': None}
        except ToolkitError as exc:
            return {'ok': False, 'data': None, 'error': {'code': exc.code, 'message': str(exc)}}
        except (OSError, UnicodeError) as exc:
            return {'ok': False, 'data': None, 'error': {'code': 'io_error', 'message': type(exc).__name__}}
        except Exception:
            return {'ok': False, 'data': None, 'error': {'code': 'provider_error', 'message': 'Tool provider failed'}}
