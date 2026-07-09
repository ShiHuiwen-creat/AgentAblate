import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from agentablate.models import (
    AdapterRuntimeIdentity,
    AgentConfig,
    TaskSpec,
    TrialSpec,
    VariantConfig,
)
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
        evaluator_hash="evaluator-hash",
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
    assert row["adapter_type"] == "fake"
    assert row["implementation_version"] == "0.1.0.dev0"
    assert row["evaluator_hash"] == trial.evaluator_hash
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


def test_storage_retry_updates_evaluator_hash(tmp_path: Path) -> None:
    storage = SQLiteStorage(tmp_path / "runs.sqlite3")
    original = _trial(tmp_path)
    storage.start_trial(original)
    changed = original.model_copy(update={"evaluator_hash": "new-evaluator-hash"})

    storage.start_trial(changed)

    assert storage.get_trial(changed.id)["evaluator_hash"] == "new-evaluator-hash"


def test_storage_records_canonical_runtime_json(tmp_path: Path) -> None:
    storage = SQLiteStorage(tmp_path / "runs.sqlite3")
    runtime = AdapterRuntimeIdentity(
        schema_version=1,
        executable="/tools/codex",
        executable_basename="codex",
        executable_sha256="a" * 64,
        version="codex-cli 1.0.0",
        policy=("--json", "--ephemeral"),
        ambient_skills_sha256="b" * 64,
    )
    trial = _trial(tmp_path).model_copy(update={"adapter_runtime": runtime})

    storage.start_trial(trial)

    row = storage.get_trial(trial.id)
    assert row["adapter_runtime_json"] == runtime.model_dump(mode="json")
    with closing(sqlite3.connect(storage.path)) as connection:
        stored = connection.execute(
            "SELECT adapter_runtime_json FROM trials WHERE id=?", (trial.id,)
        ).fetchone()[0]
    expected = runtime.model_dump(mode="json")
    assert stored == __import__("json").dumps(expected, sort_keys=True, separators=(",", ":"))


def test_storage_non_codex_runtime_is_empty_object(tmp_path: Path) -> None:
    storage = SQLiteStorage(tmp_path / "runs.sqlite3")
    trial = _trial(tmp_path)
    storage.start_trial(trial)

    assert storage.get_trial(trial.id)["adapter_runtime_json"] == {}


def test_claim_is_atomic_and_running_requires_explicit_recovery(tmp_path: Path) -> None:
    storage = SQLiteStorage(tmp_path / "runs.sqlite3")
    trial = _trial(tmp_path)

    owner = storage.claim_trial(trial)
    blocked = storage.claim_trial(trial)

    assert owner.status == "claimed"
    assert owner.attempt_id
    assert blocked.status == "running"
    assert blocked.attempt_id is None
    assert storage.recover_running(trial.id, stale_before="9999-01-01T00:00:00+00:00")
    recovered = storage.get_trial(trial.id)
    assert recovered["status"] == "failed"
    assert recovered["error"] == "recovered interrupted trial"


def test_stale_attempt_cannot_finish_new_owner(tmp_path: Path) -> None:
    storage = SQLiteStorage(tmp_path / "runs.sqlite3")
    trial = _trial(tmp_path)
    first = storage.claim_trial(trial)
    assert storage.recover_running(trial.id, stale_before="9999-01-01T00:00:00+00:00")

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
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 5
        columns = {row[1] for row in connection.execute("PRAGMA table_info(trials)")}
    assert "implementation_version" in columns
    assert "evaluator_hash" in columns
    assert "adapter_runtime_json" in columns


def test_storage_rejects_newer_schema_version(tmp_path: Path) -> None:
    path = tmp_path / "future.sqlite3"
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("PRAGMA user_version = 6")

    with pytest.raises(RuntimeError, match="newer than supported"):
        SQLiteStorage(path)


def test_storage_migrates_phase_one_row_without_data_loss(tmp_path: Path) -> None:
    path = tmp_path / "phase-one.sqlite3"
    storage = SQLiteStorage(path)
    trial = _trial(tmp_path)
    storage.start_trial(trial)
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("PRAGMA user_version = 5")
        connection.execute("ALTER TABLE trials RENAME TO old_trials")
        connection.execute(
            """CREATE TABLE trials AS SELECT id, experiment, agent_id, variant_id,
            task_id, repetition, status, success, duration_seconds, exit_code, error,
            started_at, completed_at, config_hash, extension_hashes, stdout, stderr,
            attempt_id, heartbeat_at, adapter_type, implementation_version,
            evaluator_hash FROM old_trials"""
        )
        connection.execute("DROP TABLE old_trials")

    migrated = SQLiteStorage(path)

    row = migrated.get_trial(trial.id)
    assert row["id"] == trial.id
    assert row["adapter_runtime_json"] == {}


def test_heartbeat_and_recovery_require_current_stale_owner(tmp_path: Path) -> None:
    storage = SQLiteStorage(tmp_path / "runs.sqlite3")
    trial = _trial(tmp_path)
    claim = storage.claim_trial(trial)

    assert storage.heartbeat(trial.id, claim.attempt_id)
    fresh = storage.get_trial(trial.id)["heartbeat_at"]
    assert not storage.recover_running(trial.id, stale_before=fresh)
    assert storage.recover_running(trial.id, stale_before="9999-01-01T00:00:00+00:00")
    assert not storage.heartbeat(trial.id, claim.attempt_id)
