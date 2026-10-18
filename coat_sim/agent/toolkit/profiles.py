"""Declarative presets: never patch bundles, execute persona, or grant permissions.

`preset_inject` selects a profile in scoped JSON only. An adapter must explicitly
consume its persona and apply `effective_capabilities` to its own registry context.
The original global authorization callback and filesystem roots remain mandatory.
"""
from . import ToolSpec, ToolkitError
from . import _state

CAPABILITIES = {'type': 'array', 'items': _state.IDENTIFIER, 'maxItems': 64}
PROFILE = {'type': 'object', 'properties': {
    'id': _state.IDENTIFIER, 'capabilities': CAPABILITIES,
    'persona': {'type': 'string', 'maxLength': 4000}},
    'required': ['id', 'capabilities', 'persona']}


def effective_capabilities(global_capabilities, profile):
    """Empty global OR preset lists disable everything; neither can expand access."""
    return frozenset(global_capabilities).intersection(profile.get('capabilities', ()))


def _validate(data):
    from .registry import validate
    try:
        validate(data, {'type': 'object', 'properties': {
            'profiles': {'type': 'array', 'items': PROFILE, 'maxItems': 100},
            'active': {'type': 'string', 'maxLength': 120}},
            'required': ['profiles', 'active']})
        ids = [p['id'] for p in data['profiles']]
        if len(ids) != len(set(ids)) or (data['active'] and data['active'] not in ids):
            raise ValueError('Invalid profile ids')
    except (ToolkitError, ValueError):
        raise ToolkitError('invalid_state', 'Invalid profile configuration; existing state preserved') from None
    return data


def _initial(data):
    return _validate(data) if data is not None else {'profiles': [], 'active': ''}


def _result(ctx, state):
    data = _validate(state['data'])
    return {'revision': state['revision'], 'scope': state['scope'], 'active': data['active'],
            'profiles': [{**p, 'effective_capabilities': sorted(effective_capabilities(ctx.allowed_capabilities, p))}
                         for p in data['profiles']],
            'application': 'declarative_only'}


def preset_create(ctx, args):
    def update(data):
        data = _initial(data)
        if any(p['id'] == args['id'] for p in data['profiles']):
            raise ToolkitError('conflict', 'Preset already exists')
        if len(data['profiles']) >= 100:
            raise ToolkitError('limit_exceeded', 'Maximum 100 profiles per scope')
        data['profiles'].append({'id': args['id'], 'capabilities': sorted(set(args['capabilities'])),
                                 'persona': args.get('persona', '')})
        return data
    return _result(ctx, _state.mutate(ctx, args, 'profiles', update))


def preset_list(ctx, args):
    path, scope = _state.location(ctx, args, 'profiles')
    state, _ = _state.load(ctx, path, scope, 'profiles')
    return _result(ctx, state or {'revision': 0, 'scope': scope, 'data': _initial(None)})


def _change(ctx, args, operation):
    def update(data):
        data = _initial(data)
        if operation == 'eject':
            data['active'] = ''
            return data
        if not any(p['id'] == args['id'] for p in data['profiles']):
            raise ToolkitError('not_found', 'Preset does not exist')
        if operation == 'inject':
            data['active'] = args['id']
        elif operation == 'delete':
            data['profiles'] = [p for p in data['profiles'] if p['id'] != args['id']]
            if data['active'] == args['id']:
                data['active'] = ''
        return data
    return _result(ctx, _state.mutate(ctx, args, 'profiles', update))


def preset_delete(ctx, args):
    return _change(ctx, args, 'delete')


def preset_inject(ctx, args):
    return _change(ctx, args, 'inject')


def preset_eject(ctx, args):
    return _change(ctx, args, 'eject')


def register_profiles(registry):
    specs = [
        ('preset_create', preset_create, _state.schema(PROFILE['properties'], ['id', 'capabilities'], True)),
        ('preset_list', preset_list, _state.schema()),
        ('preset_delete', preset_delete, _state.schema({'id': _state.IDENTIFIER}, ['id'], True)),
        ('preset_inject', preset_inject, _state.schema({'id': _state.IDENTIFIER}, ['id'], True)),
        ('preset_eject', preset_eject, _state.schema(mutation=True)),
    ]
    for name, handler, parameters in specs:
        registry.register(ToolSpec(name, 'preset', 'Scoped declarative profile: ' + name,
                                  parameters, effect='read' if name == 'preset_list' else 'write'), handler)


register_profile_tools = register_profiles
