"""Small, bounded lifecycle diagnostics; never serialize endpoint credentials."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from typing import Any, TextIO

from coat_sim.project_logging import ProjectLog

_FIELDS = frozenset({'operation','role','attempt','elapsed_s','exit_code','child_pid','assistant_pid',
    'client_pid','generation','version','git_sha','source_id','remote_version','remote_source_id',
    'protocol','state','window','reason','enabled','native_trace','stack','ok','window_type','action'})

def _clean(value: Any, depth: int = 0) -> Any:
    if depth > 3:
        return '<bounded>'
    if value is None or isinstance(value,(bool,int,float)):
        return value
    if isinstance(value,(list,tuple)):
        return [_clean(item,depth+1) for item in value[:12]]
    text=str(value)[:2000]
    # Quoted values can contain spaces, delimiters, escaped quotes and newlines.
    # Treat a truncated/unclosed quote as secret through the end of the buffer.
    secret_value = r"""(?:"(?:\\(?:.|$)|[^"\\])*(?:"|$)|'(?:\\(?:.|$)|[^'\\])*(?:'|$)|[^\s,"';}]+)"""
    # One pass avoids a Bearer replacement changing nested quote boundaries.
    secret_prefix = r"""(?is)\b(?:(authorization|x-coat-token|api[_-]?key|authkey|token|secret|password)\b["']?\s*[:=]\s*(?:Bearer\s+)?|Bearer\s+)"""
    text=re.sub(secret_prefix + secret_value,
                lambda match: (match.group(1) + '=' if match.group(1) else 'Bearer ') + '<redacted>',text)
    text=re.sub(r'https?://[^\s]+','<url>',text)
    text=re.sub(r'\b(?:sk-(?:proj-)?|gh[pousr]_)[A-Za-z0-9_-]{16,}','<redacted>',text)
    home=str(Path.home())
    if len(home)>3:
        text=text.replace(home,'%USERPROFILE%')
    return text[:1200]

def source_id(root: str | Path) -> str:
    return hashlib.sha256(os.path.normcase(str(Path(root).resolve())).encode()).hexdigest()[:16]

def endpoint_summary(endpoint: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any]={name:_clean(endpoint[name]) for name in ('protocol','host','pid','role','window_type','parent_pid') if name in endpoint}
    if isinstance(endpoint.get('root'),str):
        result['root_id']=source_id(endpoint['root'])
    return result

def trace(event: str, *, error: BaseException | None = None, stream: TextIO | ProjectLog | None = None, **values: Any) -> None:
    try:
        row={'timestamp':datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z'),
             'pid':os.getpid(),'event':_clean(event)}
        row.update({key:_clean(value) for key,value in values.items() if key in _FIELDS})
        if error is not None:
            row.update(error_type=type(error).__name__,code=_clean(getattr(error,'code','')),
                       message=_clean(str(error)))
            causes: list[str] = []
            cause = error.__cause__
            while cause is not None and len(causes) < 3:
                causes.append(type(cause).__name__ + ": " + str(_clean(str(cause))))
                cause = cause.__cause__
            if causes:
                row["causes"] = causes
        target = stream if stream is not None else sys.stderr
        if target is not None:  # print(file=None) would leak diagnostics into protocol stdout.
            print(json.dumps(row,ensure_ascii=False,allow_nan=False),file=target,flush=True)
    except Exception:
        pass  # A missing/full/closed log must never change control completion.

def client_trace(root: Path, event: str, *, error: BaseException | None = None, **values: Any) -> None:
    try:
        with ProjectLog("assistant-client", root) as stream:
            trace(event, error=error, stream=stream, **values)
    except OSError:
        trace(event, error=error, **values)


@contextmanager
def lifecycle_log(root: Path):
    with ProjectLog("assistant", root) as stream:
        previous=(sys.stdout,sys.stderr)
        sys.stdout=sys.stderr=stream
        try:
            yield stream
        finally:
            sys.stdout,sys.stderr=previous
