"""Canonical host-owned project binding shared by worker entry and runtime.

Keep this type outside executable modules: runpy / ``python -m`` execute worker
as __main__, which must not create a second class identity for runtime guards.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
from pathlib import Path
from typing import Any

_PROJECT_TARGET_FIELDS = frozenset({"client_id", "client_generation", "project_id",
                                    "session_generation", "root", "project_file"})


def validated_project_target(value: Any) -> dict[str, str] | None:
    """Validate only host protocol data; never discover or choose a project."""
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != _PROJECT_TARGET_FIELDS:
        raise ValueError("工程绑定须为宿主明确选择的完整六字段 target。")
    if any(not isinstance(item, str) or not item or item != item.strip()
           or len(item) > (4096 if key in {"root", "project_file"} else 128)
           or any(ord(char) < 32 for char in item) for key, item in value.items()):
        raise ValueError("工程绑定字段无效。")
    if (not Path(value['root']).is_absolute() or not Path(value['project_file']).is_absolute()
            or Path(value['project_file']).suffix.lower() != '.coatproj'):
        raise ValueError("工程绑定须包含绝对目录与明确 .coatproj 文件。")
    return dict(value)


class ProjectMemoryBinding:
    """A fixed, revocable host selection, never a model-selected project ID.

    Authentication is deliberately performed in the runtime's worker threads.
    A failed observation latches this request closed, including close/reopen.
    """
    def __init__(self, target: dict[str, str] | None, current: Any, clients: Any = None):
        self._target = validated_project_target(target)
        self._current = current
        self._clients = clients
        self._revoked = threading.Event()
        self.project_id = self._target['project_id'] if self._target else ''
        identity = ({key: (os.path.normcase(str(Path(self._target[key]).resolve()))
                           if key in {'root', 'project_file'} else self._target[key])
                     for key in ('project_id', 'root', 'project_file')} if self._target else None)
        self.memory_project_id = ('project:' + hashlib.sha256(json.dumps(
            identity, sort_keys=True, ensure_ascii=False).encode()).hexdigest()) if identity else ''

    @property
    def target(self) -> dict[str, str] | None:
        return dict(self._target) if self._target else None

    def current(self) -> bool:
        try:
            valid = not self._revoked.is_set() and self._current() is True
        except Exception:
            valid = False
        if not valid:
            self._revoked.set()
        return valid

    def verify(self) -> bool:
        if not self.current():
            return False
        if self._target is None:
            return True
        try:
            from coat_sim.assistant_process import AssistantProcessClient
            from coat_sim.project_activation import activation_target
            clients = self._clients() if self._clients is not None else AssistantProcessClient().list_clients()
            matches = [activation_target(client, project) for client in clients
                       if client.get('state') == 'alive' for project in client.get('project_sessions', [])
                       if project.get('project_id') == self.project_id
                       and activation_target(client, project) == self._target]
            valid = len(matches) == 1 and self.current()
        except Exception:
            valid = False
        if not valid:
            self._revoked.set()
        return valid

    def providers(self) -> dict[str, Any]:
        if self._target is None:
            return {}
        return {'memory_project_id': self.memory_project_id,
                'memory_project_live': self.verify,
                'memory_project_target': self.target}
