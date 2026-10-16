import json
from pathlib import Path
import tempfile
from coat_sim.core.atomic_io import replace_with_retry

class _LimitedTextWriter:
    def __init__(self, stream, max_bytes):
        self.stream = stream
        self.max_bytes = max_bytes
        self.bytes_written = 0

    def write(self, value):
        encoded_bytes = len(value.encode('utf-8'))
        if self.bytes_written + encoded_bytes > self.max_bytes:
            raise ValueError('json_output_exceeds_byte_limit')
        written = self.stream.write(value)
        if written != len(value):
            raise OSError('short_json_write')
        self.bytes_written += encoded_bytes
        return written


def write_json(path, value, *, max_bytes=None):
    if max_bytes is not None and (type(max_bytes) is not int or max_bytes < 1):
        raise ValueError('invalid_json_output_byte_limit')
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                         suffix='.tmp', delete=False) as stream:
            temporary = Path(stream.name)
            target = _LimitedTextWriter(stream, max_bytes) if max_bytes is not None else stream
            json.dump(value, target, ensure_ascii=False, allow_nan=False)
            stream.flush()
            import os
            os.fsync(stream.fileno())
        replace_with_retry(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def write_json_new(path, value, *, max_bytes=None, indent=None, final_newline=False):
    """Write bounded UTF-8 JSON to a new path without replacing an existing file.

    Serialization is completed in a sibling temporary file first. The final
    hard-link operation is exclusive, so byte-limit, serialization, and existing
    destination failures leave the requested path untouched.
    """
    if max_bytes is not None and (type(max_bytes) is not int or max_bytes < 1):
        raise ValueError('invalid_json_output_byte_limit')
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', newline='\n',
                                         dir=path.parent, suffix='.tmp', delete=False) as stream:
            temporary = Path(stream.name)
            target = _LimitedTextWriter(stream, max_bytes) if max_bytes is not None else stream
            json.dump(value, target, ensure_ascii=False, allow_nan=False, indent=indent)
            if final_newline:
                target.write('\n')
            stream.flush()
            import os
            os.fsync(stream.fileno())
        import os
        os.link(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)

def write_text(path, value):
    """Atomically persist already canonical text without another JSON encoding."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                     suffix='.tmp', delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(value)
        stream.flush()
        import os
        os.fsync(stream.fileno())
    try:
        replace_with_retry(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
