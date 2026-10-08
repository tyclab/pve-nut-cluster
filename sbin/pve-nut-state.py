#!/usr/bin/env python3
"""Durably checkpoint local UPS recovery intent before a guest state mutation."""

import os
from pathlib import Path
import sys
import tempfile


def sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def update(mode, path, data):
    if mode == "move":
        destination = Path(data.strip())
        if destination.parent != path.parent:
            raise ValueError("recovery archives must stay on the same local filesystem")
        path.rename(destination)
        sync_directory(path.parent)
        return
    if mode == "append" and path.exists():
        data = path.read_text() + data
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + ".tmp-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        Path(temporary).unlink(missing_ok=True)


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] not in {"write", "append", "move"}:
        sys.exit("usage: pve-nut-state.py write|append|move path < value")
    update(sys.argv[1], Path(sys.argv[2]), sys.stdin.read())
