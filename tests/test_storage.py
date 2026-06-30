import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from agentablate.models import AgentConfig, TaskSpec, TrialSpec, VariantConfig
from agentablate.storage import SQLiteStorage


def _trial(tmp_path: Path) -> TrialSpec:
    return TrialSpec(
        id="trial-1",
        experiment="demo",
        agent=AgentConfig(id="fake", adapter="fake"),
        variant=VariantConfig(id="baseline", skills=(tmp_path / "secret-skill",)),
        task=TaskSpec(
            id="task-1",
            repo=tmp_path / "secret-repo",
            prompt="work",
            test_command=("true",),
        ),
        repetition=0,
        timeout_seconds=1,
        config_hash="config-hash",
        extension_hashes=("extension-hash",),
    )


def test_storage_records_portable_experiment_and_completed_trial(tmp_path: Path) -> None:
    storage = SQLiteStorage(tmp_path / "runs.sqlite3")
    trial = _trial(tmp_path)

    storage.register_experiment("demo", "config-hash", tmp_path / "config.yml")
    attempt_id = storage.start_trial(trial)
    storage.finish_trial(
        trial,
        attempt_id,
        status="completed",
        success=True,
        duration_seconds=1.25,
        exit_code=0,
        error=None,
        stdout="ok",
        stderr="",
    )

    experiment = storage.get_experiment("demo")
    row = storage.get_trial(trial.id)
    assert experiment == {
        "name": "demo",
        "config_hash": "config-hash",
        "source": "config.yml",
        "created_at": experiment["created_at"],
    }
    assert row["status"] == "completed"
    assert row["success"] == 1
    assert row["stdout"] == "ok"
    assert row["config_hash"] == "config-hash"
    assert row["extension_hashes"] == '["extension-hash"]'
    assert storage.is_completed(trial.id)
    assert str(tmp_path) not in repr(experiment) + repr(row)


def test_storage_restarts_incomplete_trial_and_uses_utc_timestamps(tmp_path: Path) -> None:
    storage = SQLiteStorage(tmp_path / "runs.sqlite3")
    trial = _trial(tmp_path)

    storage.start_trial(trial)
    first = storage.get_trial(trial.id)
    storage.start_trial(trial)
    restarted = storage.get_trial(trial.id)

    assert restarted["status"] == "running"
    assert restarted["completed_at"] is None
    assert first["started_at"].endswith("+00:00")
    assert restarted["started_at"].endswith("+00:00")


def test_claim_is_atomic_and_running_requires_explicit_recovery(tmp_path: Path) -> None:
    storage = SQLiteStorage(tmp_path / "runs.sqlite3")
    trial = _trial(tmp_path)

    owner = storage.claim_trial(trial)
    blocked = storage.claim_trial(trial)

    assert owner.status == "claimed"
    assert owner.attempt_id
    assert blocked.status == "running"
    assert blocked.attempt_id is None
    assert storage.recover_running(trial.id)
    recovered = storage.get_trial(trial.id)
    assert recovered["status"] == "failed"
    assert recovered["error"] == "recovered interrupted trial"


def test_stale_attempt_cannot_finish_new_owner(tmp_path: Path) -> None:
    storage = SQLiteStorage(tmp_path / "runs.sqlite3")
    trial = _trial(tmp_path)
    first = storage.claim_trial(trial)
    assert storage.recover_running(trial.id)

    stale_after_recovery = storage.finish_trial(
        trial,
        first.attempt_id,
        status="completed",
        success=True,
        duration_seconds=1,
        exit_code=0,
        error=None,
        stdout="stale",
        stderr="",
    )
    assert not stale_after_recovery
    assert storage.get_trial(trial.id)["status"] == "failed"

    second = storage.claim_trial(trial)
    stale_after_reclaim = storage.finish_trial(
        trial,
        first.attempt_id,
        status="completed",
        success=True,
        duration_seconds=1,
        exit_code=0,
        error=None,
        stdout="stale",
        stderr="",
    )
    current = storage.get_trial(trial.id)

    assert not stale_after_reclaim
    assert current["status"] == "running"
    assert current["attempt_id"] == second.attempt_id


def test_storage_migrates_legacy_schema_before_writing(tmp_path: Path) -> None:
    path = tmp_path / "legacy.sqlite3"
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.executescript(
            """
            CREATE TABLE experiments (
                name TEXT PRIMARY KEY, config_hash TEXT, source TEXT, created_at TEXT
            );
            CREATE TABLE trials (
                id TEXT PRIMARY KEY, experiment TEXT, agent_id TEXT, variant_id TEXT,
                task_id TEXT, repetition INTEGER, status TEXT, success INTEGER,
                duration_seconds REAL, exit_code INTEGER, error TEXT, started_at TEXT,
                completed_at TEXT
            );
            """
        )
    storage = SQLiteStorage(path)
    trial = _trial(tmp_path)

    claim = storage.claim_trial(trial)
    assert claim.status == "claimed"
    storage.finish_trial(
        trial,
        claim.attempt_id,
        status="completed",
        success=True,
        duration_seconds=0,
        exit_code=0,
        error=None,
        stdout="out",
        stderr="err",
    )

    row = storage.get_trial(trial.id)
    assert row["config_hash"] == trial.config_hash
    assert row["stdout"] == "out"
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2


def test_storage_rejects_newer_schema_version(tmp_path: Path) -> None:
    path = tmp_path / "future.sqlite3"
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("PRAGMA user_version = 3")

    with pytest.raises(RuntimeError, match="newer than supported"):
        SQLiteStorage(path)
