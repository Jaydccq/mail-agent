"""Private, consistent SQLite snapshots; validation restores into an isolated database."""

from __future__ import annotations

import os
import secrets
import sqlite3
import tempfile
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

RETAIN_BACKUPS = 7
REQUIRED_TABLES = ("messages", "todos", "rules", "memories", "outbox", "kv")


def _read_only(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)


def verify_backup(path: Path) -> dict[str, int]:
    """Restore a snapshot elsewhere, check integrity and app tables, return counts only."""
    with tempfile.TemporaryDirectory(prefix="mailagent-restore-") as tmp:
        restored = Path(tmp) / "restored.db"
        with closing(_read_only(path)) as source, closing(sqlite3.connect(restored)) as dest:
            source.backup(dest, pages=256)
            if dest.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise ValueError("backup integrity check failed")
            if dest.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise ValueError("backup foreign key check failed")
            return {table: dest.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                    for table in REQUIRED_TABLES}


def create_backup(source: Path, directory: Path | None = None) -> Path:
    """Include committed WAL data, validate before publishing, then retain seven snapshots."""
    directory = directory if directory is not None else source.parent / "backups"
    if directory.is_symlink():
        raise ValueError("backup directory must not be a symlink")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    fd, name = tempfile.mkstemp(prefix=".snapshot-", dir=directory)
    os.close(fd)  # mkstemp creates mode 0600; SQLite uses this existing private file
    temporary = Path(name)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    target = directory / f"mail-agent-{stamp}-{secrets.token_hex(4)}.sqlite3"
    try:
        with closing(_read_only(source)) as src, closing(sqlite3.connect(temporary)) as dst:
            src.backup(dst, pages=256)
            dst.execute("PRAGMA journal_mode=DELETE")
        verify_backup(temporary)
        with temporary.open("rb") as snapshot:
            os.fsync(snapshot.fileno())
        temporary.replace(target)
        for old in sorted(directory.glob("mail-agent-*.sqlite3"), reverse=True)[RETAIN_BACKUPS:]:
            if old.is_file() and not old.is_symlink():
                old.unlink()
        return target
    finally:
        temporary.unlink(missing_ok=True)
