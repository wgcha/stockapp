"""Local operator job status and offline circuit reset, without API credentials."""
from __future__ import annotations
from datetime import datetime, timezone
from pathlib import Path
import sqlite3

from .data_lock import RuntimeDataLock, checked_path
from .scheduler import DEFAULT_JOBS, SQLiteJobStateStore


def job_status(data_dir: str | Path) -> list[dict]:
    path = checked_path(Path(data_dir) / "jobs.sqlite3")
    if not path.is_file():
        raise ValueError("No job database; initialize the bot first")
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        rows = [dict(row) for row in connection.execute("SELECT * FROM job_states ORDER BY job_id")]
    finally:
        connection.close()
    definitions = {item.job_id: item for item in DEFAULT_JOBS}
    now = datetime.now(timezone.utc)
    for row in rows:
        definition = definitions.get(row["job_id"])
        if row.get("execution_started") and definition and definition.execution_timeout:
            row["execution_overdue"] = (now - datetime.fromisoformat(row["execution_started"]) > definition.execution_timeout)
    return rows


def reset_job(data_dir: str | Path, job_id: str) -> None:
    with RuntimeDataLock(data_dir):
        rows = job_status(data_dir)
        enabled = {row["job_id"] for row in rows}
        if job_id not in enabled:
            raise ValueError("Unknown scheduled job")
        definitions = tuple(item for item in DEFAULT_JOBS if item.job_id in enabled)
        if job_id not in {item.job_id for item in definitions}:
            raise ValueError("Unsupported scheduled job")
        now = datetime.now(timezone.utc)
        store = SQLiteJobStateStore(Path(data_dir) / "jobs.sqlite3", start=now, definitions=definitions)
        store.recover_interrupted(now)
        store.reset(job_id, now)
