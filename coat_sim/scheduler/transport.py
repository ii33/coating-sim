"""Versioned transport seam. Remote deployment security belongs to a future adapter."""
from typing import Protocol
from pathlib import Path
import hashlib
from .io import replace_with_retry

class Transport(Protocol):
    def request(self, action: str, arguments: dict) -> dict: ...
    def download(self, artifact_id: str, destination) -> Path: ...

def download_verified(opener, url, destination, metadata):
    import tempfile
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(); count = 0
    with tempfile.NamedTemporaryFile(mode='wb', dir=destination.parent, delete=False) as target:
        temporary = Path(target.name)
        try:
            with opener.open(url, timeout=60) as response:
                for block in iter(lambda: response.read(1024*1024), b''):
                    target.write(block); digest.update(block); count += len(block)
        except BaseException:
            target.close(); temporary.unlink(missing_ok=True); raise
    try:
        if count != metadata['bytes'] or digest.hexdigest() != metadata['sha256']:
            raise ValueError('artifact_integrity_failed')
        replace_with_retry(temporary, destination)
    finally: temporary.unlink(missing_ok=True)
    return destination
