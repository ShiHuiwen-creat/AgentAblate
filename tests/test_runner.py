import asyncio
import json
from pathlib import Path

import pytest

from agentablate.adapters.base import AdapterResult, AdapterTimeout, AgentEvent
from agentablate.evaluators import EvaluationResult
from agentablate.models import AgentConfig, TaskSpec, TrialSpec, VariantConfig
from agentablate.runner import TrialRunner
from agentablate.storage import SQLiteStorage


def _trial(tmp_path: Path, trial_id: str = "trial-1", adapter: str = "fake") -> TrialSpec:
    return TrialSpec(
        id=trial_id,
        experiment="demo",
        agent=AgentConfig(id=adapter, adapter=adapter),
        variant=VariantConfig(id="baseline"),
        task=TaskSpec(
            id="task-1",
            repo=tmp_path / "repo",
            prompt="work",
            test_command=("true",),
        ),
        repetition=0,
        timeout_seconds=2,
        config_hash="config-hash",
        extension_hashes=("extension-hash",),
    )


class RecordingWorkspace:
    def __init__(self, repo: Path, revision: str, path: Path, calls: list[str]) -> None:
        self.path = path
        self.calls = calls

    async def __aenter__(self) -> "RecordingWorkspace":
        self.path.mkdir(parents=True)
        self.calls.append("workspace")
        return self

    async def __aexit__(self, *args: object) -> None:
        self.calls.append("cleanup")


class RecordingAdapter:
    def __init__(self, calls: list[str], result: AdapterResult | BaseException) -> None:
        self.calls = calls
        self.result = result

    async def run(self, trial: TrialSpec, cwd: Path) -> AdapterResult:
        self.calls.append("adapter")
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


@pytest.mark.asyncio
async def test_trial_runs_workspace_adapter_evaluator_and_persists_matching_events(
    tmp_path: Path,
) -> None:
    calls: list[str] = []
    trial = _trial(tmp_path)
    adapter_result = AdapterResult(
        0, (AgentEvent("message", 1.0, {"text": "ok"}),), "agent out", "agent err"
    )

    async def evaluator(task: TaskSpec, path: Path, timeout: float) -> EvaluationResult:
        calls.append("evaluator")
        return EvaluationResult(True, 0, "tests out", "", ("changed.txt",))

    runner = TrialRunner(
        tmp_path,
        SQLiteStorage(tmp_path / "runs.sqlite3"),
        adapters={"fake": lambda trial: RecordingAdapter(calls, adapter_result)},
        workspace_factory=lambda repo, revision, path: RecordingWorkspace(
            repo, revision, path, calls
        ),
        evaluator=evaluator,
    )

    row = await runner.run_trial(trial)

    events = [json.loads(line) for line in runner.events_path(trial.id).read_text().splitlines()]
    assert calls == ["workspace", "adapter", "evaluator", "cleanup"]
    assert row["status"] == "completed"
    assert row["success"] == 1
    assert row["stdout"] == "agent out\ntests out"
    assert {event["trial_id"] for event in events} == {trial.id}
    assert runner.storage.get_trial(trial.id)["id"] == events[0]["trial_id"]
    experiment = runner.storage.get_experiment(trial.experiment)
    assert experiment["config_hash"] == trial.config_hash
    assert str(tmp_path) not in experiment["source"]


@pytest.mark.asyncio
async def test_adapter_failure_is_stored_with_partial_result_and_batch_continues(
    tmp_path: Path,
) -> None:
    calls: list[str] = []
    partial = AdapterResult(
        -1, (AgentEvent("timeout", 2.0, {"partial": True}),), "partial out", "partial err"
    )
    trials = (_trial(tmp_path, "failed"), _trial(tmp_path, "completed"))

    def adapter_factory(trial: TrialSpec) -> RecordingAdapter:
        result: AdapterResult | BaseException = (
            AdapterTimeout(partial)
            if trial.id == "failed"
            else AdapterResult(0, (), "ok", "")
        )
        return RecordingAdapter(calls, result)

    async def evaluator(task: TaskSpec, path: Path, timeout: float) -> EvaluationResult:
        return EvaluationResult(True, 0, "", "", ())

    runner = TrialRunner(
        tmp_path,
        SQLiteStorage(tmp_path / "runs.sqlite3"),
        adapters={"fake": adapter_factory},
        workspace_factory=lambda repo, revision, path: RecordingWorkspace(
            repo, revision, path, calls
        ),
        evaluator=evaluator,
    )

    rows = await runner.run_all(trials, concurrency=2)

    assert [row["status"] for row in rows] == ["failed", "completed"]
    failed = runner.storage.get_trial("failed")
    assert failed["error"].startswith("AdapterTimeout:")
    assert failed["stdout"] == "partial out"
    assert failed["stderr"] == "partial err"
    assert "partial" in runner.events_path("failed").read_text()


@pytest.mark.asyncio
async def test_completed_trial_is_skipped(tmp_path: Path) -> None:
    calls: list[str] = []
    trial = _trial(tmp_path)
    storage = SQLiteStorage(tmp_path / "runs.sqlite3")
    storage.start_trial(trial)
    storage.finish_trial(
        trial,
        status="completed",
        success=True,
        duration_seconds=0,
        exit_code=0,
        error=None,
        stdout="old",
        stderr="",
    )
    runner = TrialRunner(
        tmp_path,
        storage,
        adapters={"fake": lambda trial: RecordingAdapter(calls, RuntimeError("no"))},
        workspace_factory=lambda repo, revision, path: RecordingWorkspace(
            repo, revision, path, calls
        ),
    )

    row = await runner.run_trial(trial)

    assert row["stdout"] == "old"
    assert calls == []


@pytest.mark.asyncio
async def test_cancellation_persists_failure_and_cleans_workspace(tmp_path: Path) -> None:
    calls: list[str] = []
    trial = _trial(tmp_path)

    class BlockingAdapter:
        async def run(self, trial: TrialSpec, cwd: Path) -> AdapterResult:
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    runner = TrialRunner(
        tmp_path,
        SQLiteStorage(tmp_path / "runs.sqlite3"),
        adapters={"fake": lambda trial: BlockingAdapter()},
        workspace_factory=lambda repo, revision, path: RecordingWorkspace(
            repo, revision, path, calls
        ),
    )
    running = asyncio.create_task(runner.run_trial(trial))
    await asyncio.sleep(0)
    running.cancel()

    with pytest.raises(asyncio.CancelledError):
        await running

    row = runner.storage.get_trial(trial.id)
    assert row["status"] == "failed"
    assert row["error"].startswith("CancelledError:")
    assert calls[-1] == "cleanup"


@pytest.mark.asyncio
async def test_run_all_enforces_concurrency_limit(tmp_path: Path) -> None:
    active = 0
    maximum = 0

    class LimitedAdapter:
        async def run(self, trial: TrialSpec, cwd: Path) -> AdapterResult:
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            await asyncio.sleep(0.02)
            active -= 1
            return AdapterResult(0, (), "", "")

    async def evaluator(task: TaskSpec, path: Path, timeout: float) -> EvaluationResult:
        return EvaluationResult(True, 0, "", "", ())

    calls: list[str] = []
    runner = TrialRunner(
        tmp_path,
        SQLiteStorage(tmp_path / "runs.sqlite3"),
        adapters={"fake": lambda trial: LimitedAdapter()},
        workspace_factory=lambda repo, revision, path: RecordingWorkspace(
            repo, revision, path, calls
        ),
        evaluator=evaluator,
    )

    await runner.run_all(tuple(_trial(tmp_path, f"trial-{index}") for index in range(5)), 2)

    assert maximum == 2


@pytest.mark.asyncio
async def test_command_adapter_requires_command_configuration(tmp_path: Path) -> None:
    trial = _trial(tmp_path, adapter="command")
    runner = TrialRunner(
        tmp_path,
        SQLiteStorage(tmp_path / "runs.sqlite3"),
        workspace_factory=lambda repo, revision, path: RecordingWorkspace(
            repo, revision, path, []
        ),
    )

    row = await runner.run_trial(trial)

    assert row["status"] == "failed"
    assert "requires agent.command" in row["error"]
