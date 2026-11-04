"""A private rolling JSONL file containing a configurable number of accepted messages."""
from collections import deque
import json
import os
from pathlib import Path
import tempfile

MAX_ENTRIES = 10


def read(directory: Path, limit=None, identity=None, session=None, *, max_entries=MAX_ENTRIES,
         name=None, epoch=None):
    """Latest matching retained records in chronological order.

    identity matches the record "id" (the generated name for new records, or a
    legacy identity ID); name matches the generated name; epoch disambiguates
    resets. An absent or concurrently replaced file is an empty history.
    """
    if limit is None:
        limit = max_entries
    path = directory / "history.jsonl"
    recent = deque(maxlen=max_entries)
    try:
        stream = path.open("rb")
    except FileNotFoundError:
        return []
    with stream:
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
                and (name is None or str(record.get("name", "")).lower() == name.lower())
                and (epoch is None or record.get("epoch") == epoch)
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
