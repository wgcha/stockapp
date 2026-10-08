"""Single-process ownership shared by the bot and offline maintenance commands."""
from __future__ import annotations

import os
from pathlib import Path


def checked_path(path: str | Path) -> Path:
    candidate = Path(os.path.abspath(path))
    for part in (candidate, *candidate.parents):
        if part.is_symlink() or getattr(part, "is_junction", lambda: False)():
            raise ValueError("Linked data paths are not supported")
    return candidate


class RuntimeDataLock:
    """OS file lock: automatically released if a process exits or crashes."""

    def __init__(self, data_dir: str | Path):
        self.data_dir = checked_path(data_dir)
        self._file = None

    def __enter__(self):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        lock_path = checked_path(self.data_dir / ".runtime.lock")
        stream = open(lock_path, "a+b")
        try:
            stream.seek(0, os.SEEK_END)
            if stream.tell() == 0:
                stream.write(b"0")
                stream.flush()
            stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            stream.close()
            raise RuntimeError("Data directory is in use; stop the bot before maintenance") from None
        self._file = stream
        return self

    def __exit__(self, *exc):
        if self._file is not None:
            stream, self._file = self._file, None
            try:
                stream.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            finally:
                stream.close()
