from pathlib import Path

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
    storage.start_trial(trial)
    storage.finish_trial(
        trial,
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

