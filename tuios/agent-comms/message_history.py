"""A private rolling JSONL file containing a configurable number of accepted messages."""
from collections import deque
import json
import os
from pathlib import Path
import tempfile

MAX_ENTRIES = 10


def read(directory: Path, limit=None, identity=None, session=None, *, max_entries=MAX_ENTRIES):
    if limit is None:
        limit = max_entries
    path = directory / "history.jsonl"
    if not path.exists():
        return []
    recent = deque(maxlen=max_entries)
    with path.open("rb") as stream:
        for line in stream:
            if not line.endswith(b"\n"):
                continue
            try:
                record = json.loads(line)
            except (ValueError, UnicodeError):
                continue
            if isinstance(record, dict) and all(isinstance(record.get(k), str) for k in ("id", "name", "msg", "created_at")):
                recent.append(record)
    matching = [record for record in recent
                if (identity is None or record.get("id") == identity)
                and (session is None or record.get("session_name") == session)]
    return matching[-limit:]


def append(directory: Path, message: dict, *, max_entries=MAX_ENTRIES):
    """Caller holds history.lock. Replace the data file, never the lock inode."""
    records = [*read(directory, max_entries=max_entries), message][-max_entries:]
    fd, filename = tempfile.mkstemp(prefix=".history-", suffix=".jsonl", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            for record in records:
                stream.write(json.dumps(record, ensure_ascii=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(filename, directory / "history.jsonl")
    finally:
        Path(filename).unlink(missing_ok=True)
