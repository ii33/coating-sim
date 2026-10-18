"""Portable task ledger. Rendered context is data, never backend instructions."""
from typing import Any

from . import ToolSpec, ToolkitError
from . import _state

STATUSES = ['todo', 'doing', 'done', 'blocked']
TASK: dict[str, Any] = {'type': 'object', 'properties': {
    'id': _state.IDENTIFIER, 'text': _state.TEXT,
    'status': {'type': 'string', 'enum': STATUSES}, 'note': _state.TEXT},
    'required': ['id', 'text']}


def _validate(data):
    from .registry import validate
    try:
        validate(data, {'type': 'object', 'properties': {
            'goal': _state.TEXT, 'stage': _state.TEXT,
            'tasks': {'type': 'array', 'items': TASK, 'maxItems': 200}},
            'required': ['goal', 'stage', 'tasks']})
        ids = [task['id'] for task in data['tasks']]
        if len(ids) != len(set(ids)):
            raise ValueError('duplicate')
    except (ToolkitError, ValueError):
        raise ToolkitError('invalid_state', 'Invalid task ledger; existing state preserved') from None
    return data


def _required(data):
    if data is None:
        raise ToolkitError('not_found', 'Initialize a plan first')
    return _validate(data)


def _clip(text, limit):
    suffix = '\n… Use task_show for detail.'
    return text if len(text) <= limit else text[:limit-len(suffix)] + suffix


def render_panel(data):
    """At most 700 Unicode characters, for host-controlled dynamic context."""
    data = _validate(data)
    tasks = data['tasks']
    def short(value, length):
        value = value.replace('\n', ' ').replace('\r', ' ')
        return value if len(value) <= length else value[:length-1] + '…'
    lines = [f"Goal: {short(data['goal'], 100)}", f"Stage: {short(data['stage'], 60)}"]
    lines += [f"[{t.get('status', 'todo')}] {short(t['id'], 12)}: {short(t['text'], 42)}" for t in tasks[:6]]
    lines.append(f"Progress: {sum(t.get('status') == 'done' for t in tasks)}/{len(tasks)}")
    next_task = next((t for t in tasks if t.get('status') != 'done'), None)
    if next_task:
        lines.append('Next: ' + short(next_task['text'], 70))
    return _clip('\n'.join(lines), 700)


def _result(state):
    data = _validate(state['data'])
    lines = [f"Goal: {data['goal']}", f"Stage: {data['stage']}"]
    lines += [f"[{t.get('status', 'todo')}] {t['id']}: {t['text']} | {t.get('note', '')}" for t in data['tasks']]
    full = '\n'.join(lines)
    return {'revision': state['revision'], 'scope': state['scope'],
            'panel': render_panel(data), 'detail': _clip(full, 4000),
            'truncated': len(full) > 4000, 'task_count': len(data['tasks'])}


def plan_init(ctx, args):
    def update(previous):
        if previous is not None:
            raise ToolkitError('conflict', 'Plan exists; update it instead of replacing it')
        data = {'goal': args['goal'], 'stage': args.get('stage', ''),
                'tasks': [{**t, 'status': t.get('status', 'todo')} for t in args['tasks']]}
        return _validate(data)
    return _result(_state.mutate(ctx, args, 'tasks', update))


def task_update(ctx, args):
    def update(data):
        data = _required(data)
        task = next((t for t in data['tasks'] if t['id'] == args['id']), None)
        if task is None:
            raise ToolkitError('not_found', 'Task id does not exist')
        changes = {k: args[k] for k in ('text', 'status', 'note') if k in args}
        if not changes:
            raise ToolkitError('invalid_arguments', 'Provide text, status or note')
        task.update(changes)
        return data
    return _result(_state.mutate(ctx, args, 'tasks', update))


def stage_set(ctx, args):
    def update(data):
        data = _required(data)
        data['stage'] = args['stage']
        return data
    return _result(_state.mutate(ctx, args, 'tasks', update))


def task_show(ctx, args):
    path, scope = _state.location(ctx, args, 'tasks')
    state, _ = _state.load(ctx, path, scope, 'tasks')
    if state is None:
        return {'revision': 0, 'scope': scope, 'panel': '', 'detail': '', 'task_count': 0, 'truncated': False}
    return _result(state)


def register_tasks(registry):
    specs = [
        ('plan_init', plan_init, _state.schema({'goal': _state.TEXT, 'stage': _state.TEXT,
          'tasks': {'type': 'array', 'items': TASK, 'maxItems': 200}}, ['goal', 'tasks'], True)),
        ('task_update', task_update, _state.schema(dict(TASK['properties']), ['id'], True)),
        ('task_show', task_show, _state.schema()),
        ('stage_set', stage_set, _state.schema({'stage': _state.TEXT}, ['stage'], True)),
    ]
    for name, handler, parameters in specs:
        registry.register(ToolSpec(name, 'tasks', 'Scoped portable task ledger: ' + name,
                                  parameters, effect='read' if name == 'task_show' else 'write'), handler)


register_task_tools = register_tasks
