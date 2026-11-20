"""Local source snapshots kept outside public result JSON and released with the result."""

import hashlib
import threading
import weakref
from dataclasses import dataclass
from pathlib import Path

from autorepair.privacy import redact

MIMES = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".html": "text/html",
    ".htm": "text/html",
    ".json": "application/json",
}


@dataclass(frozen=True)
class SourceFile:
    data: bytes
    suffix: str
    name: str

    @property
    def digest(self):
        return hashlib.sha256(self.data).hexdigest()


def read_source(path):
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix not in MIMES:
        raise ValueError("不支持保存该原件格式")
    return SourceFile(path.read_bytes(), suffix, redact(path.name))


_pending = {}
_lock = threading.RLock()


def capture_source(result, path):
    try:
        source = read_source(path)
    except (OSError, ValueError):
        return
    if source.digest != result.document.source_hash or result.status == "failed":
        return
    identity = id(result)

    def release(_):
        with _lock:
            _pending.pop(identity, None)

    with _lock:
        _pending[identity] = (weakref.ref(result, release), source)


def source_snapshot(result):
    with _lock:
        item = _pending.get(id(result))
        return item[1] if item and item[0]() is result else None
