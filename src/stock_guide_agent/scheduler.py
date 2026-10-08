from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from contextlib import contextmanager
from pathlib import Path
import sqlite3
from typing import Callable, Iterator, Literal
import threading
import time


ExecutionMode = Literal["api_only", "llm_research"]


@dataclass(frozen=True)
class JobDefinition:
    job_id: str
    interval: timedelta
    execution_mode: ExecutionMode
    max_consecutive_failures: int = 3
    circuit_cooldown: timedelta = timedelta(minutes=15)
    execution_timeout: timedelta | None = timedelta(minutes=5)

    def __post_init__(self) -> None:
        if self.interval.total_seconds() <= 0:
            raise ValueError("job interval must be positive")
        if self.circuit_cooldown.total_seconds() <= 0:
            raise ValueError("circuit cooldown must be positive")
        if self.max_consecutive_failures <= 0:
            raise ValueError("max failures must be positive")
        if self.execution_timeout is not None and self.execution_timeout.total_seconds() <= 0:
            raise ValueError("execution timeout must be positive")


@dataclass
class JobState:
    definition: JobDefinition
    next_due: datetime
    last_attempt: datetime | None = None
    last_success: datetime | None = None
    consecutive_failures: int = 0
    circuit_open: bool = False
    circuit_half_open: bool = False
    execution_started: datetime | None = None
    execution_overdue: bool = False

    def due(self, now: datetime) -> bool:
        _require_aware(now)
        if self.execution_started is not None:
            return False
        if self.circuit_half_open:
            return False
        return now >= self.next_due

    def begin_probe(self, now: datetime) -> None:
        _require_aware(now)
        if self.circuit_open and now < self.next_due:
            raise RuntimeError("circuit cooldown has not elapsed")
        self.circuit_half_open = self.circuit_open
        self.execution_started = now
        self.execution_overdue = False

    def finish_execution(self, now: datetime) -> None:
        _require_aware(now)
        if self.execution_started is not None and self.definition.execution_timeout:
            elapsed = now - self.execution_started
            self.execution_overdue = elapsed > self.definition.execution_timeout
        self.execution_started = None

    def mark_success(self, now: datetime) -> None:
        _require_aware(now)
        self.last_attempt = now
        self.last_success = now
        self.consecutive_failures = 0
        self.circuit_open = False
        self.circuit_half_open = False
        self.execution_started = None
        self.next_due = now + self.definition.interval

    def mark_failure(self, now: datetime) -> None:
        _require_aware(now)
        self.last_attempt = now
        self.consecutive_failures += 1
        self.circuit_open = (
            self.consecutive_failures >= self.definition.max_consecutive_failures
        )
        backoff_multiplier = min(8, 2 ** (self.consecutive_failures - 1))
        if self.circuit_open:
            self.circuit_half_open = False
            self.next_due = now + self.definition.circuit_cooldown
        else:
            self.next_due = now + self.definition.interval * backoff_multiplier
        self.execution_started = None

    def reset_circuit(self, now: datetime) -> None:
        _require_aware(now)
        self.consecutive_failures = 0
        self.circuit_open = False
        self.circuit_half_open = False
        self.execution_started = None
        self.execution_overdue = False
        self.next_due = now


def _require_aware(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("scheduler datetimes must be timezone-aware")


def initialize_jobs(start: datetime) -> dict[str, JobState]:
    _require_aware(start)
    return {
        definition.job_id: JobState(definition, next_due=start)
        for definition in DEFAULT_JOBS
    }


def due_jobs(states: dict[str, JobState], now: datetime) -> list[JobState]:
    _require_aware(now)
    return sorted(
        (state for state in states.values() if state.due(now)),
        key=lambda state: (state.definition.execution_mode, state.definition.job_id),
    )


@dataclass(frozen=True)
class JobRunResult:
    job_id: str
    success: bool
    detail: str = ""


class SQLiteJobStateStore:
    def __init__(
        self,
        path: str | Path,
        *,
        start: datetime,
        definitions: tuple[JobDefinition, ...] = (),
    ) -> None:
        _require_aware(start)
        self.path = str(path)
        self.definitions = {
            item.job_id: item for item in (definitions or DEFAULT_JOBS)
        }
        self._initialize(start)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def _initialize(self, start: datetime) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS job_states (
                    job_id TEXT PRIMARY KEY,
                    next_due TEXT NOT NULL,
                    last_attempt TEXT,
                    last_success TEXT,
                    consecutive_failures INTEGER NOT NULL,
                    circuit_open INTEGER NOT NULL
                    ,circuit_half_open INTEGER NOT NULL DEFAULT 0
                    ,execution_started TEXT
                    ,execution_overdue INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(job_states)")}
            for name, definition in (
                ("circuit_half_open", "INTEGER NOT NULL DEFAULT 0"),
                ("execution_started", "TEXT"),
                ("execution_overdue", "INTEGER NOT NULL DEFAULT 0"),
            ):
                if name not in columns:
                    connection.execute(f"ALTER TABLE job_states ADD COLUMN {name} {definition}")
            for job_id in self.definitions:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO job_states(
                        job_id, next_due, consecutive_failures, circuit_open
                    ) VALUES (?, ?, 0, 0)
                    """,
                    (job_id, start.isoformat()),
                )

    def load(self) -> dict[str, JobState]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM job_states ORDER BY job_id"
            ).fetchall()
        states: dict[str, JobState] = {}
        for row in rows:
            job_id = str(row["job_id"])
            definition = self.definitions.get(job_id)
            if definition is None:
                continue
            states[job_id] = JobState(
                definition=definition,
                next_due=datetime.fromisoformat(str(row["next_due"])),
                last_attempt=(
                    datetime.fromisoformat(str(row["last_attempt"]))
                    if row["last_attempt"]
                    else None
                ),
                last_success=(
                    datetime.fromisoformat(str(row["last_success"]))
                    if row["last_success"]
                    else None
                ),
                consecutive_failures=int(row["consecutive_failures"]),
                circuit_open=bool(row["circuit_open"]),
                circuit_half_open=bool(row["circuit_half_open"]) if "circuit_half_open" in row.keys() else False,
                execution_started=(datetime.fromisoformat(str(row["execution_started"])) if row["execution_started"] else None) if "execution_started" in row.keys() else None,
                execution_overdue=bool(row["execution_overdue"]) if "execution_overdue" in row.keys() else False,
            )
        return states

    def save(self, state: JobState) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE job_states SET next_due = ?, last_attempt = ?,
                    last_success = ?, consecutive_failures = ?, circuit_open = ?,
                    circuit_half_open = ?, execution_started = ?, execution_overdue = ?
                WHERE job_id = ?
                """,
                (
                    state.next_due.isoformat(),
                    state.last_attempt.isoformat() if state.last_attempt else None,
                    state.last_success.isoformat() if state.last_success else None,
                    state.consecutive_failures,
                    int(state.circuit_open),
                    int(state.circuit_half_open),
                    state.execution_started.isoformat() if state.execution_started else None,
                    int(state.execution_overdue),
                    state.definition.job_id,
                ),
            )

    def status(self, job_id: str | None = None, *, now: datetime | None = None) -> dict[str, JobState] | JobState:
        """Return persisted state; an unknown job id is always an error."""
        states = self.load()
        current = now or datetime.now(timezone.utc)
        _require_aware(current)
        for state in states.values():
            if state.execution_started is not None and state.definition.execution_timeout is not None:
                state.execution_overdue = current - state.execution_started > state.definition.execution_timeout
        if job_id is None:
            return states
        if job_id not in self.definitions:
            raise KeyError(f"unknown scheduled job: {job_id}")
        return states[job_id]

    def reset(self, job_id: str, now: datetime) -> JobState:
        _require_aware(now)
        if job_id not in self.definitions:
            raise KeyError(f"unknown scheduled job: {job_id}")
        state = self.load()[job_id]
        if state.execution_started is not None or state.circuit_half_open:
            raise RuntimeError("Cannot reset a running job")
        state.reset_circuit(now)
        self.save(state)
        return state

    def recover_interrupted(self, now: datetime) -> tuple[str, ...]:
        """Call once at startup while holding exclusive application data lock."""
        recovered = []
        for state in self.load().values():
            if state.execution_started is not None or state.circuit_half_open:
                state.mark_failure(now)
                state.circuit_half_open = False
                self.save(state)
                recovered.append(state.definition.job_id)
        return tuple(recovered)


JobHandler = Callable[[datetime], str | None]


class ScheduledJobRunner:
    def __init__(
        self,
        store: SQLiteJobStateStore,
        handlers: dict[str, JobHandler],
    ) -> None:
        unknown = set(handlers) - set(store.definitions)
        if unknown:
            raise ValueError(f"unknown scheduled job handlers: {sorted(unknown)}")
        self.store = store
        self.handlers = handlers
        self._run_lock = threading.Lock()

    def run_due(self, now: datetime) -> tuple[JobRunResult, ...]:
        _require_aware(now)
        if not self._run_lock.acquire(blocking=False):
            return ()
        try:
            return self._run_due_locked(now)
        finally:
            self._run_lock.release()

    def _run_due_locked(self, now: datetime) -> tuple[JobRunResult, ...]:
        states = self.store.load()
        results: list[JobRunResult] = []
        batch_started = time.monotonic()
        for state in due_jobs(states, now):
            handler = self.handlers.get(state.definition.job_id)
            if handler is None:
                continue
            job_now = now + timedelta(seconds=max(0.0, time.monotonic() - batch_started))
            state.begin_probe(job_now)
            self.store.save(state)
            started = time.monotonic()
            try:
                detail = handler(job_now) or ""
            except Exception:
                finished = job_now + timedelta(seconds=max(0.0, time.monotonic() - started))
                state.finish_execution(finished)
                state.mark_failure(finished)
                results.append(
                    JobRunResult(
                        state.definition.job_id,
                        False,
                        "scheduled handler failed",
                    )
                )
            else:
                finish = job_now
                # Handlers receive a logical scheduler time; use wall time only
                # for timeout classification without attempting to kill them.
                if state.definition.execution_timeout:
                    finish = job_now + timedelta(seconds=max(0.0, time.monotonic() - started))
                state.finish_execution(finish)
                state.mark_success(finish)
                results.append(JobRunResult(state.definition.job_id, True, detail))
            self.store.save(state)
        return tuple(results)


DEFAULT_JOBS = (
    JobDefinition("toss_invest_market_data", timedelta(minutes=1), "api_only"),
    JobDefinition("daily_user_digest", timedelta(minutes=5), "api_only"),
    JobDefinition("toss_account_risk", timedelta(minutes=1), "api_only"),
    JobDefinition("paper_strategy_evaluation", timedelta(hours=1), "api_only"),
    JobDefinition("volatility_model_evaluation", timedelta(hours=1), "api_only"),
    JobDefinition("long_term_model_evaluation", timedelta(hours=1), "api_only"),
    JobDefinition("intraday_bar_archive", timedelta(hours=1), "api_only"),
    JobDefinition("paper_portfolio_valuation", timedelta(hours=1), "api_only"),
    JobDefinition("open_dart_disclosures", timedelta(minutes=5), "api_only"),
    JobDefinition("toss_invest_investor_flow", timedelta(minutes=15), "api_only"),
    JobDefinition("toss_invest_overseas", timedelta(minutes=15), "api_only"),
    JobDefinition("naver_material_news", timedelta(minutes=30), "api_only"),
    JobDefinition("ecos_macro", timedelta(days=1), "api_only"),
    JobDefinition("strategy_health_check", timedelta(days=7), "api_only"),
    JobDefinition("strategy_validation", timedelta(days=30), "api_only"),
    JobDefinition("academic_strategy_discovery", timedelta(days=30), "api_only"),
    JobDefinition("external_strategy_research", timedelta(days=90), "llm_research"),
)
