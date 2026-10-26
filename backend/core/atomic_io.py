"""Crash-safe writes for the small JSON state files under var/db/ (2026-10-26).

The paper engines keep their live state (open positions, the crypto account's capital) in one JSON file next to the database. A
plain `Path.write_text()` truncates the file BEFORE writing it, so a crash, a full disk or a `systemctl restart` in that window left
a half-written file -- and the crypto engine then rebuilt its state from the built-in defaults, silently resetting the account to
its starting money and overwriting the database's capital to match.

`write_json` writes a temp file in the same directory, fsyncs it, copies the previous contents to `<name>.bak` and swaps the temp
file in with `os.replace` (atomic on POSIX), so readers only ever see a complete file.
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path


def backup_path(path: Path) -> Path:
    return path.with_name(path.name + ".bak")


def write_json(path: Path, data, indent: int = 2) -> None:
    """Write `data` as JSON to `path` atomically, keeping the previous version in `<path>.bak`."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(data, indent=indent))
        fh.flush()
        os.fsync(fh.fileno())
    if path.exists():
        try:
            shutil.copy2(path, backup_path(path))
        except OSError:            # a missing backup must never block the real write
            pass
    os.replace(tmp, path)


def read_json(path: Path) -> dict:
    """The JSON object at `path`, or {} when the file is missing/unreadable/not an object. Never raises."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}
