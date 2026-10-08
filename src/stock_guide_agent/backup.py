"""Offline, checked SQLite snapshots. Restores never overwrite existing data."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile

from .data_lock import RuntimeDataLock, checked_path


DATABASE_NAMES = frozenset({
    "strategies.sqlite3", "market_cache.sqlite3", "users.sqlite3", "paper_trading.sqlite3",
    "intraday_bars.sqlite3", "research.sqlite3", "messages.sqlite3",
    "source_fingerprints.sqlite3", "jobs.sqlite3",
})


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _check_database(path: Path) -> None:
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    try:
        if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise ValueError("Database integrity check failed")
    finally:
        connection.close()


def _target(source: Path, destination: str | Path) -> Path:
    target = checked_path(destination)
    if target.exists():
        raise ValueError("Destination must be a new directory")
    if source == target or source in target.parents or target in source.parents:
        raise ValueError("Source and destination must be separate directory trees")
    if not target.parent.is_dir():
        raise ValueError("Destination parent directory must exist")
    return target


def _cleanup(stage: Path, parent: Path) -> None:
    # Only remove the private staging directory we created, never user data.
    if checked_path(stage).parent != parent or not stage.name.startswith(".stock-guide-stage-"):
        raise ValueError("Unexpected staging path")
    shutil.rmtree(stage)


def backup_databases(data_dir: str | Path, destination: str | Path) -> dict:
    source = checked_path(data_dir)
    if not source.is_dir():
        raise ValueError("Data directory does not exist")
    target = _target(source, destination)
    with RuntimeDataLock(source):
        if any(path.name not in DATABASE_NAMES for path in source.glob("*.sqlite3")):
            raise ValueError("Unknown database file; update the backup registry before proceeding")
        names = sorted(name for name in DATABASE_NAMES if (source / name).exists())
        if not names:
            raise ValueError("No application databases to back up")
        stage = Path(tempfile.mkdtemp(prefix=".stock-guide-stage-", dir=target.parent))
        try:
            manifest = {"version": 1, "created_at": datetime.now(timezone.utc).isoformat(), "files": {}}
            for name in names:
                original = checked_path(source / name)
                if not original.is_file():
                    raise ValueError("Expected a database file")
                _check_database(original)
                reader = sqlite3.connect(original.as_uri() + "?mode=ro", uri=True)
                writer = sqlite3.connect(stage / name)
                try:
                    reader.backup(writer)
                finally:
                    writer.close()
                    reader.close()
                saved = stage / name
                _check_database(saved)
                manifest["files"][name] = {"size": saved.stat().st_size, "sha256": _hash(saved)}
            (stage / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
            if target.exists():
                raise ValueError("Destination already exists")
            stage.rename(target)
            return manifest
        finally:
            if stage.exists():
                _cleanup(stage, target.parent)


def restore_databases(backup_dir: str | Path, target_dir: str | Path) -> dict:
    source = checked_path(backup_dir)
    target = _target(source, target_dir)
    manifest_path = checked_path(source / "manifest.json")
    if not manifest_path.is_file() or manifest_path.stat().st_size > 65536:
        raise ValueError("Missing or oversized backup manifest")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or manifest.get("version") != 1:
        raise ValueError("Unsupported backup manifest")
    files = manifest.get("files")
    if not isinstance(files, dict) or not files or not set(files).issubset(DATABASE_NAMES):
        raise ValueError("Invalid backup file list")
    stage = Path(tempfile.mkdtemp(prefix=".stock-guide-stage-", dir=target.parent))
    try:
        for name, metadata in files.items():
            original = checked_path(source / name)
            if not isinstance(metadata, dict) or not original.is_file():
                raise ValueError("Invalid backup file metadata")
            saved = stage / name
            shutil.copyfile(original, saved)
            if saved.stat().st_size != metadata.get("size") or _hash(saved) != metadata.get("sha256"):
                raise ValueError("Backup checksum mismatch")
            _check_database(saved)
        if target.exists():
            raise ValueError("Destination already exists")
        stage.rename(target)
        return manifest
    finally:
        if stage.exists():
            _cleanup(stage, target.parent)
