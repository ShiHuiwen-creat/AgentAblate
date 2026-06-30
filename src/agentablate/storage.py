import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agentablate.models import TrialSpec


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


class SQLiteStorage:
    """Transactional SQLite persistence for experiments and trial state."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connection() as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version > 1:
                raise RuntimeError(
                    f"database schema version {version} is newer than supported version 1"
                )
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """CREATE TABLE IF NOT EXISTS experiments (
                    name TEXT PRIMARY KEY,
                    config_hash TEXT NOT NULL,
                    source TEXT NOT NULL,
                    created_at TEXT NOT NULL
                    )"""
                )
                connection.execute(
                    """CREATE TABLE IF NOT EXISTS trials (
                    id TEXT PRIMARY KEY,
                    experiment TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    variant_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    repetition INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    success INTEGER,
                    duration_seconds REAL,
                    exit_code INTEGER,
                    error TEXT,
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    config_hash TEXT NOT NULL,
                    extension_hashes TEXT NOT NULL,
                    stdout TEXT NOT NULL DEFAULT '',
                    stderr TEXT NOT NULL DEFAULT ''
                    )"""
                )
                experiment_columns = {
                    row[1]
                    for row in connection.execute("PRAGMA table_info(experiments)")
                }
                required_experiment_columns = {
                    "name", "config_hash", "source", "created_at"
                }
                if missing := required_experiment_columns - experiment_columns:
                    raise RuntimeError(
                        "experiments schema is missing columns: "
                        f"{', '.join(sorted(missing))}"
                    )
                columns = {
                    row[1]
                    for row in connection.execute("PRAGMA table_info(trials)")
                }
                additions = {
                    "config_hash": "TEXT NOT NULL DEFAULT ''",
                    "extension_hashes": "TEXT NOT NULL DEFAULT '[]'",
                    "stdout": "TEXT NOT NULL DEFAULT ''",
                    "stderr": "TEXT NOT NULL DEFAULT ''",
                }
                for name, definition in additions.items():
                    if name not in columns:
                        connection.execute(
                            f"ALTER TABLE trials ADD COLUMN {name} {definition}"
                        )
                required = {
                    "id", "experiment", "agent_id", "variant_id", "task_id",
                    "repetition", "status", "success", "duration_seconds",
                    "exit_code", "error", "started_at", "completed_at",
                    *additions,
                }
                migrated = {
                    row[1]
                    for row in connection.execute("PRAGMA table_info(trials)")
                }
                if missing := required - migrated:
                    raise RuntimeError(
                        f"trials schema is missing columns: {', '.join(sorted(missing))}"
                    )
                connection.execute("PRAGMA user_version = 1")
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    def register_experiment(self, name: str, config_hash: str, source: str | Path) -> None:
        portable_source = Path(source).name
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO experiments(name, config_hash, source, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    config_hash=excluded.config_hash,
                    source=excluded.source
                """,
                (name, config_hash, portable_source, _utc_now()),
            )

    def start_trial(self, trial: TrialSpec) -> None:
        with self._connection() as connection:
            self._write_start(connection, trial)

    def _write_start(
        self, connection: sqlite3.Connection, trial: TrialSpec
    ) -> None:
        values = (
            trial.id,
            trial.experiment,
            trial.agent.id,
            trial.variant.id,
            trial.task.id,
            trial.repetition,
            "running",
            _utc_now(),
            trial.config_hash,
            json.dumps(trial.extension_hashes, separators=(",", ":")),
        )
        connection.execute(
            """
                INSERT INTO trials(
                    id, experiment, agent_id, variant_id, task_id, repetition,
                    status, started_at, config_hash, extension_hashes
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    status='running', success=NULL, duration_seconds=NULL,
                    exit_code=NULL, error=NULL, started_at=excluded.started_at,
                    completed_at=NULL, stdout='', stderr=''
            """,
            values,
        )

    def claim_trial(
        self, trial: TrialSpec, *, recover_running: bool = False
    ) -> str:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status FROM trials WHERE id=?", (trial.id,)
            ).fetchone()
            if row is not None:
                if row["status"] == "completed":
                    return "completed"
                if row["status"] == "running" and not recover_running:
                    return "running"
            self._write_start(connection, trial)
            return "claimed"

    def finish_trial(
        self,
        trial: TrialSpec,
        *,
        status: str,
        success: bool | None,
        duration_seconds: float,
        exit_code: int | None,
        error: str | None,
        stdout: str,
        stderr: str,
    ) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE trials SET status=?, success=?, duration_seconds=?, exit_code=?,
                    error=?, completed_at=?, stdout=?, stderr=? WHERE id=?
                """,
                (
                    status,
                    success,
                    duration_seconds,
                    exit_code,
                    error,
                    _utc_now(),
                    stdout,
                    stderr,
                    trial.id,
                ),
            )

    def is_completed(self, trial_id: str) -> bool:
        row = self.get_trial(trial_id)
        return row is not None and row["status"] == "completed"

    def get_experiment(self, name: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM experiments WHERE name=?", (name,)
            ).fetchone()
        return dict(row) if row is not None else None

    def get_trial(self, trial_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM trials WHERE id=?", (trial_id,)
            ).fetchone()
        return dict(row) if row is not None else None
