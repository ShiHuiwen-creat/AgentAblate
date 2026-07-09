import asyncio
import warnings
from pathlib import Path

import pytest
import yaml

from agentablate.adapters.codex_exec import CODEX_EXEC_POLICY, CodexExecAdapter
from agentablate.experiment import (
    ApplicationError,
    _adapter,
    doctor_experiment,
    initialize_experiment,
    run_experiment,
    starter_documents,
)
from agentablate.models import (
    AdapterRuntimeIdentity,
    AgentConfig,
    ExperimentBundle,
    ExperimentConfig,
    ExperimentMeta,
)


def _bundle(tmp_path: Path) -> ExperimentBundle:
    return ExperimentBundle(
        source=tmp_path / "agentablate.yaml",
        config_hash="hash",
        config=ExperimentConfig(
            version=1,
            experiment=ExperimentMeta(name="demo"),
            agents=(AgentConfig(id="fake", adapter="fake"),),
            variants=(),
            tasks=(),
        ),
        tasks=(),
    )


def test_doctor_calls_adapter_doctor(tmp_path: Path) -> None:
    calls: list[str] = []

    class Adapter:
        async def doctor(self) -> tuple[bool, str]:
            calls.append("doctor")
            return True, "ready"

    results = doctor_experiment(
        tmp_path / "agentablate.yaml",
        loader=lambda path: _bundle(tmp_path),
        adapter_factory=lambda agent: Adapter(),
    )

    assert calls == ["doctor"]
    assert [(result.agent_id, result.available) for result in results] == [("fake", True)]


def test_doctor_registry_selects_native_codex_adapter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "codex"
    executable.write_bytes(b"codex")
    runtime = AdapterRuntimeIdentity(
        schema_version=1,
        executable=executable,
        executable_basename="codex",
        executable_sha256="a" * 64,
        version="v",
        policy=CODEX_EXEC_POLICY,
        ambient_skills_sha256="b" * 64,
    )
    monkeypatch.setattr("agentablate.experiment.discover_codex_runtime", lambda: runtime)

    selected = _adapter(AgentConfig(id="codex", adapter="codex-exec"))

    assert isinstance(selected, CodexExecAdapter)
    assert selected.runtime is runtime


def test_run_forwards_concurrency_and_resume_to_runner(tmp_path: Path) -> None:
    observed: dict[str, object] = {}

    class Runner:
        def __init__(self, root, storage, *, recover_running):
            observed["recover_running"] = recover_running

        async def run_all(self, trials, concurrency):
            observed["concurrency"] = concurrency
            return []

    summary = run_experiment(
        tmp_path / "agentablate.yaml",
        concurrency=7,
        resume=False,
        loader=lambda path: _bundle(tmp_path),
        matrix_expander=lambda bundle: [],
        runner_factory=Runner,
        allow_empty=True,
    )

    assert observed == {"recover_running": False, "concurrency": 7}
    assert summary.total == 0


@pytest.mark.parametrize("operation", [doctor_experiment, run_experiment])
def test_sync_workflows_reject_a_running_event_loop_without_leaking_coroutines(
    operation,
    tmp_path: Path,
) -> None:
    async def invoke() -> None:
        with warnings.catch_warnings():
            warnings.simplefilter("error", RuntimeWarning)
            with pytest.raises(ApplicationError, match="running event loop"):
                operation(tmp_path / "agentablate.yaml")

    asyncio.run(invoke())


def test_starter_task_round_trips_python_path_with_spaces_and_windows_syntax() -> None:
    executable = r"C:\Program Files\Python 3\python.exe"
    config_text, task_text = starter_documents(executable)

    config = yaml.safe_load(config_text)
    task = yaml.safe_load(task_text)

    assert config["tasks"] == ["./task.yaml"]
    assert task["test_command"][0] == executable


def test_init_preserves_unrelated_old_fixed_staging_directory(tmp_path: Path) -> None:
    unrelated = tmp_path / ".agentablate-init-tmp"
    unrelated.mkdir()
    (unrelated / "owned.txt").write_text("keep", encoding="utf-8")
    config, task = starter_documents()

    initialize_experiment(tmp_path, config, task, False)

    assert (unrelated / "owned.txt").read_text(encoding="utf-8") == "keep"


@pytest.mark.parametrize("failed_replace", [2, 3])
def test_force_init_rolls_back_all_targets_when_install_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failed_replace: int,
) -> None:
    config, task = starter_documents()
    initialize_experiment(tmp_path, config, task, False)
    (tmp_path / "agentablate.yaml").write_text("old config", encoding="utf-8")
    (tmp_path / "task.yaml").write_text("old task", encoding="utf-8")
    (tmp_path / "fixture" / "owned.txt").write_text("old fixture", encoding="utf-8")
    original_replace = Path.replace
    calls = 0

    def fail_once(source: Path, destination: Path):
        nonlocal calls
        calls += 1
        if calls == failed_replace:
            raise OSError("injected replace failure")
        original_replace(source, destination)

    monkeypatch.setattr(Path, "replace", fail_once)

    with pytest.raises(ApplicationError, match="injected replace failure"):
        initialize_experiment(tmp_path, config, task, True)

    assert (tmp_path / "agentablate.yaml").read_text(encoding="utf-8") == "old config"
    assert (tmp_path / "task.yaml").read_text(encoding="utf-8") == "old task"
    assert (tmp_path / "fixture" / "owned.txt").read_text(encoding="utf-8") == "old fixture"
