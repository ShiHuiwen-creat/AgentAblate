import asyncio
import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

import pytest

from agentablate.adapters.base import AdapterCancelled, AdapterResult, AdapterTimeout, AgentEvent
from agentablate.adapters.codex_exec import CODEX_EXEC_POLICY, CodexExecAdapter
from agentablate.adapters.command import CommandAdapter
from agentablate.evaluators import EvaluationResult
from agentablate.models import (
    AdapterRuntimeIdentity,
    AgentConfig,
    TaskSpec,
    TrialSpec,
    VariantConfig,
)
from agentablate.runner import AdapterConfigurationError, TrialRunner
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
        self.path.mkdir(parents=True, exist_ok=True)
        self.calls.append("workspace")
        return self

    async def __aexit__(self, *args: object) -> None:
        self.calls.append("cleanup")


def test_runner_registry_selects_native_runtime_and_rejects_missing_runtime(tmp_path: Path) -> None:
    runner = TrialRunner(tmp_path, SQLiteStorage(tmp_path / "runs.sqlite3"))
    trial = _trial(tmp_path, adapter="codex-exec")
    with pytest.raises(AdapterConfigurationError, match="adapter_runtime"):
        runner._adapter_for(trial)
    runtime = AdapterRuntimeIdentity(
        schema_version=1,
        executable=tmp_path / "codex",
        executable_basename="codex",
        executable_sha256="a" * 64,
        version="v",
        policy=CODEX_EXEC_POLICY,
        ambient_skills_sha256="b" * 64,
    )

    selected = runner._adapter_for(trial.model_copy(update={"adapter_runtime": runtime}))

    assert isinstance(selected, CodexExecAdapter)
    assert selected.runtime is runtime


class RecordingAdapter:
    def __init__(self, calls: list[str], result: AdapterResult | BaseException) -> None:
        self.calls = calls
        self.result = result

    async def run(self, trial: TrialSpec, cwd: Path) -> AdapterResult:
        self.calls.append("adapter")
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


def _codex_trial_with_runtime(tmp_path: Path, trial_id: str = "codex-trial") -> TrialSpec:
    runtime = AdapterRuntimeIdentity(
        schema_version=1,
        executable=tmp_path / "codex",
        executable_basename="codex",
        executable_sha256="a" * 64,
        version="v",
        policy=CODEX_EXEC_POLICY,
        ambient_skills_sha256="b" * 64,
    )
    return _trial(tmp_path, trial_id, adapter="codex-exec").model_copy(
        update={"adapter_runtime": runtime}
    )


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
async def test_codex_mcp_is_rejected_before_adapter_factory(tmp_path: Path) -> None:
    trial = _codex_trial_with_runtime(tmp_path).model_copy(
        update={"variant": VariantConfig(id="with-mcp", mcp=(tmp_path / "server.json",))}
    )
    adapter_called = False
    workspace_called = False

    def adapter_factory(trial: TrialSpec) -> RecordingAdapter:
        nonlocal adapter_called
        adapter_called = True
        return RecordingAdapter([], AdapterResult(0, (), "", ""))

    def workspace_factory(
        repo: Path, revision: str, path: Path
    ) -> RecordingWorkspace:
        nonlocal workspace_called
        workspace_called = True
        return RecordingWorkspace(repo, revision, path, [])

    runner = TrialRunner(
        tmp_path,
        SQLiteStorage(tmp_path / "runs.sqlite3"),
        adapters={"codex-exec": adapter_factory},
        workspace_factory=workspace_factory,
    )

    with pytest.raises(AdapterConfigurationError, match="MCP"):
        await runner._execute_trial(trial, [], [], [])

    assert not adapter_called
    assert not workspace_called


@pytest.mark.asyncio
async def test_variant_skills_are_visible_to_adapter_and_removed_before_evaluator(
    tmp_path: Path,
) -> None:
    skill = tmp_path / "skill"
    skill.mkdir()
    (skill / "SKILL.md").write_text(
        "---\nname: reviewer\ndescription: Test\n---\n\nUse it.\n",
        encoding="utf-8",
    )
    from agentablate.skills import inspect_skill

    tree = inspect_skill(skill)
    trial = _trial(tmp_path, adapter="command").model_copy(
        update={
            "agent": AgentConfig(
                id="command", adapter="command", command=(sys.executable, "-c", "pass")
            ),
            "variant": VariantConfig(id="with-skill", skills=(skill,)),
            "skill_inputs": (tree.identity,),
        }
    )
    observed_during_adapter = False
    observed_during_evaluator = False

    class ObservingAdapter:
        async def run(self, trial: TrialSpec, cwd: Path) -> AdapterResult:
            nonlocal observed_during_adapter
            observed_during_adapter = (
                cwd / ".agents" / "skills" / tree.identity.install_name / "SKILL.md"
            ).is_file()
            return AdapterResult(0, (), "", "")

    async def evaluator(task: TaskSpec, path: Path, timeout: float) -> EvaluationResult:
        nonlocal observed_during_evaluator
        observed_during_evaluator = (path / ".agents" / "skills").exists()
        return EvaluationResult(True, 0, "", "", ())

    runner = TrialRunner(
        tmp_path,
        SQLiteStorage(tmp_path / "runs.sqlite3"),
        adapters={"command": lambda trial: ObservingAdapter()},
        workspace_factory=lambda repo, revision, path: RecordingWorkspace(repo, revision, path, []),
        evaluator=evaluator,
    )

    success, exit_code = await runner._execute_trial(trial, [], [], [])

    assert (success, exit_code) == (True, 0)
    assert observed_during_adapter
    assert not observed_during_evaluator


@pytest.mark.asyncio
async def test_codex_cancellation_and_skill_restore_failure_are_both_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    skill = tmp_path / "skill"
    skill.mkdir()
    (skill / "SKILL.md").write_text(
        "---\nname: reviewer\ndescription: Test\n---\n\nUse it.\n",
        encoding="utf-8",
    )
    from agentablate.skills import inspect_skill

    tree = inspect_skill(skill)
    trial = _codex_trial_with_runtime(tmp_path).model_copy(
        update={
            "variant": VariantConfig(id="with-skill", skills=(skill,)),
            "skill_inputs": (tree.identity,),
        }
    )
    cancelled = AdapterCancelled(AdapterResult(-1, (), "partial", "err"))
    cleanup = OSError("restore failed")

    class FailingAdapter:
        async def run(self, trial: TrialSpec, cwd: Path) -> AdapterResult:
            raise cancelled

    monkeypatch.setattr(
        "agentablate.skills._restore",
        lambda state: (_ for _ in ()).throw(cleanup),
    )
    runner = TrialRunner(
        tmp_path,
        SQLiteStorage(tmp_path / "runs.sqlite3"),
        adapters={"codex-exec": lambda trial: FailingAdapter()},
        workspace_factory=lambda repo, revision, path: RecordingWorkspace(repo, revision, path, []),
    )

    with pytest.raises(BaseExceptionGroup) as raised:
        await runner._execute_trial(trial, [], [], [])

    assert list(raised.value.exceptions) == [cancelled, cleanup]


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
            AdapterTimeout(partial) if trial.id == "failed" else AdapterResult(0, (), "ok", "")
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
    attempt_id = storage.start_trial(trial)
    storage.finish_trial(
        trial,
        attempt_id,
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
async def test_command_cancellation_persists_partial_streams_and_events(
    tmp_path: Path,
) -> None:
    calls: list[str] = []
    marker = tmp_path / "agent-started"
    script = (
        "import pathlib, sys, time; "
        "print('partial stdout', flush=True); "
        "print('partial stderr', file=sys.stderr, flush=True); "
        f"pathlib.Path({str(marker)!r}).write_text('ready'); "
        "time.sleep(10)"
    )
    trial = _trial(tmp_path, adapter="command").model_copy(
        update={
            "agent": AgentConfig(
                id="command", adapter="command", command=(sys.executable, "-c", script)
            )
        }
    )
    runner = TrialRunner(
        tmp_path,
        SQLiteStorage(tmp_path / "runs.sqlite3"),
        workspace_factory=lambda repo, revision, path: RecordingWorkspace(
            repo, revision, path, calls
        ),
    )
    running = asyncio.create_task(runner.run_trial(trial))
    async with asyncio.timeout(3):
        while not marker.exists():
            await asyncio.sleep(0.01)

    in_progress = [
        json.loads(line) for line in runner.events_path(trial.id).read_text().splitlines()
    ]
    assert [event["kind"] for event in in_progress] == ["start"]

    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running

    row = runner.storage.get_trial(trial.id)
    events = [json.loads(line) for line in runner.events_path(trial.id).read_text().splitlines()]
    assert row["status"] == "failed"
    assert row["error"].startswith("AdapterCancelled:")
    assert row["stdout"] == "partial stdout\n"
    assert row["stderr"] == "partial stderr\n"
    assert [event["kind"] for event in events] == ["start", "cancelled"]
    assert {event["trial_id"] for event in events} == {trial.id}
    assert calls[-1] == "cleanup"


@pytest.mark.asyncio
async def test_retry_replaces_failed_attempt_events(tmp_path: Path) -> None:
    calls: list[str] = []
    trial = _trial(tmp_path)
    attempts = 0

    def adapter_factory(trial: TrialSpec) -> RecordingAdapter:
        nonlocal attempts
        attempts += 1
        event = AgentEvent(f"attempt-{attempts}", float(attempts), {})
        if attempts == 1:
            result = AdapterResult(-1, (event,), "old", "")
            return RecordingAdapter(calls, AdapterTimeout(result))
        return RecordingAdapter(calls, AdapterResult(0, (event,), "new", ""))

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

    assert (await runner.run_trial(trial))["status"] == "failed"
    assert (await runner.run_trial(trial))["status"] == "completed"

    events = [json.loads(line) for line in runner.events_path(trial.id).read_text().splitlines()]
    assert [event["kind"] for event in events] == ["attempt-2"]


@pytest.mark.asyncio
async def test_evaluator_cancellation_persists_partial_streams(tmp_path: Path) -> None:
    marker = tmp_path / "evaluation-started"
    script = (
        "import pathlib, sys, time; "
        "print('evaluation stdout', flush=True); "
        "print('evaluation stderr', file=sys.stderr, flush=True); "
        f"pathlib.Path({str(marker)!r}).write_text('ready'); "
        "time.sleep(10)"
    )
    task = _trial(tmp_path).task.model_copy(update={"test_command": (sys.executable, "-c", script)})
    trial = _trial(tmp_path).model_copy(update={"task": task})
    calls: list[str] = []
    runner = TrialRunner(
        tmp_path,
        SQLiteStorage(tmp_path / "runs.sqlite3"),
        workspace_factory=lambda repo, revision, path: RecordingWorkspace(
            repo, revision, path, calls
        ),
    )
    running = asyncio.create_task(runner.run_trial(trial))
    async with asyncio.timeout(3):
        while not marker.exists():
            await asyncio.sleep(0.01)

    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running

    row = runner.storage.get_trial(trial.id)
    assert row["status"] == "failed"
    assert row["error"].startswith("EvaluationCancelled:")
    assert "evaluation stdout" in row["stdout"]
    assert row["stderr"] == "evaluation stderr\n"
    assert calls[-1] == "cleanup"


@pytest.mark.asyncio
async def test_duplicate_trial_id_in_batch_executes_once(tmp_path: Path) -> None:
    executions = 0

    class BlockingAdapter:
        async def run(self, trial: TrialSpec, cwd: Path) -> AdapterResult:
            nonlocal executions
            executions += 1
            await asyncio.sleep(0.02)
            return AdapterResult(0, (), "", "")

    async def evaluator(task: TaskSpec, path: Path, timeout: float) -> EvaluationResult:
        return EvaluationResult(True, 0, "", "", ())

    calls: list[str] = []
    trial = _trial(tmp_path)
    runner = TrialRunner(
        tmp_path,
        SQLiteStorage(tmp_path / "runs.sqlite3"),
        adapters={"fake": lambda trial: BlockingAdapter()},
        workspace_factory=lambda repo, revision, path: RecordingWorkspace(
            repo, revision, path, calls
        ),
        evaluator=evaluator,
    )

    rows = await runner.run_all((trial, trial), concurrency=2)

    assert executions == 1
    assert {row["status"] for row in rows} <= {"running", "completed"}


@pytest.mark.asyncio
async def test_run_all_contains_storage_failure_to_one_trial(tmp_path: Path) -> None:
    class FailingStorage(SQLiteStorage):
        def claim_trial(self, trial: TrialSpec):
            if trial.id == "broken":
                raise sqlite3.OperationalError("database is busy")
            return super().claim_trial(trial)

    calls: list[str] = []
    runner = TrialRunner(
        tmp_path,
        FailingStorage(tmp_path / "runs.sqlite3"),
        adapters={"fake": lambda trial: RecordingAdapter(calls, AdapterResult(0, (), "", ""))},
        workspace_factory=lambda repo, revision, path: RecordingWorkspace(
            repo, revision, path, calls
        ),
        evaluator=lambda task, path, timeout: asyncio.sleep(
            0, result=EvaluationResult(True, 0, "", "", ())
        ),
    )

    rows = await runner.run_all(
        (_trial(tmp_path, "broken"), _trial(tmp_path, "healthy")), concurrency=2
    )

    assert rows[0]["status"] == "failed"
    assert "database is busy" in rows[0]["error"]
    assert rows[1]["status"] == "completed"


def test_events_path_rejects_path_traversal(tmp_path: Path) -> None:
    runner = TrialRunner(tmp_path, SQLiteStorage(tmp_path / "runs.sqlite3"))

    with pytest.raises(ValueError, match="trial id"):
        runner.events_path("../escape")


@pytest.mark.asyncio
async def test_event_reset_failure_finishes_claimed_attempt(tmp_path: Path) -> None:
    trial = _trial(tmp_path)
    storage = SQLiteStorage(tmp_path / "runs.sqlite3")
    runner = TrialRunner(tmp_path, storage)
    runner.events_path(trial.id).mkdir(parents=True)

    row = await runner.run_trial(trial)

    assert row["status"] == "failed"
    assert "Error:" in row["error"]


@pytest.mark.asyncio
async def test_finish_failure_does_not_mask_cancellation(tmp_path: Path) -> None:
    class FinishFailingStorage(SQLiteStorage):
        def finish_trial(self, *args: object, **kwargs: object) -> bool:
            raise sqlite3.OperationalError("finish unavailable")

    class BlockingAdapter:
        async def run(self, trial: TrialSpec, cwd: Path) -> AdapterResult:
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    storage = FinishFailingStorage(tmp_path / "runs.sqlite3")
    runner = TrialRunner(
        tmp_path,
        storage,
        adapters={"fake": lambda trial: BlockingAdapter()},
        workspace_factory=lambda repo, revision, path: RecordingWorkspace(repo, revision, path, []),
    )
    running = asyncio.create_task(runner.run_trial(_trial(tmp_path)))
    await asyncio.sleep(0)
    running.cancel()

    with pytest.raises(asyncio.CancelledError) as captured:
        await running

    assert any("finish unavailable" in note for note in captured.value.__notes__)


@pytest.mark.asyncio
async def test_failure_error_redacts_workspace_paths(tmp_path: Path) -> None:
    calls: list[str] = []
    trial = _trial(tmp_path)
    error = RuntimeError(f"failed in {tmp_path} and {trial.task.repo}")
    runner = TrialRunner(
        tmp_path,
        SQLiteStorage(tmp_path / "runs.sqlite3"),
        adapters={"fake": lambda trial: RecordingAdapter(calls, error)},
        workspace_factory=lambda repo, revision, path: RecordingWorkspace(
            repo, revision, path, calls
        ),
    )

    row = await runner.run_trial(trial)

    assert str(tmp_path) not in row["error"]
    assert "<root>" in row["error"]


@pytest.mark.asyncio
async def test_runner_redacts_allowed_environment_secret_from_all_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "service-token-value-unique"
    script = (
        "import os; value=os.environ['SERVICE_TOKEN']; "
        "print(value); print(value, file=__import__('sys').stderr)"
    )
    trial = _trial(tmp_path, adapter="command").model_copy(
        update={
            "agent": AgentConfig(
                id="command",
                adapter="command",
                command=(sys.executable, "-c", script),
            )
        }
    )
    runner = TrialRunner(
        tmp_path,
        SQLiteStorage(tmp_path / "runs.sqlite3"),
        adapters={
            "command": lambda trial: CommandAdapter(
                trial.agent.command or (), allowed_env=("SERVICE_TOKEN",)
            )
        },
        workspace_factory=lambda repo, revision, path: RecordingWorkspace(repo, revision, path, []),
        evaluator=lambda *args: asyncio.sleep(0, result=EvaluationResult(True, 0, "", "", ())),
    )
    monkeypatch.setenv("SERVICE_TOKEN", secret)

    row = await runner.run_trial(trial)
    jsonl = runner.events_path(trial.id).read_text()

    assert secret not in row["stdout"] + row["stderr"] + jsonl
    assert "[REDACTED]" in row["stdout"]
    assert "[REDACTED]" in row["stderr"]


@pytest.mark.asyncio
async def test_runner_recursively_redacts_secret_event_keys_and_preserves_plain_text(
    tmp_path: Path,
) -> None:
    event = AgentEvent(
        "message",
        1.0,
        {
            "nested": {
                "api_KEY": "hidden",
                "monkey": "banana",
                "label": "ordinary text",
            }
        },
    )
    runner = TrialRunner(
        tmp_path,
        SQLiteStorage(tmp_path / "runs.sqlite3"),
        adapters={
            "fake": lambda trial: RecordingAdapter(
                [], AdapterResult(0, (event,), "ordinary text", "")
            )
        },
        workspace_factory=lambda repo, revision, path: RecordingWorkspace(repo, revision, path, []),
        evaluator=lambda *args: asyncio.sleep(0, result=EvaluationResult(True, 0, "", "", ())),
    )

    row = await runner.run_trial(_trial(tmp_path))
    evidence = json.loads(runner.events_path("trial-1").read_text())

    assert evidence["data"]["nested"] == {
        "api_KEY": "[REDACTED]",
        "monkey": "banana",
        "label": "ordinary text",
    }
    assert row["stdout"] == "ordinary text"


@pytest.mark.asyncio
async def test_nonzero_adapter_result_fails_without_running_evaluator(
    tmp_path: Path,
) -> None:
    evaluator_called = False

    async def evaluator(*args: object) -> EvaluationResult:
        nonlocal evaluator_called
        evaluator_called = True
        return EvaluationResult(True, 0, "would pass", "", ())

    adapter_result = AdapterResult(7, (), "partial output", "agent failed")
    runner = TrialRunner(
        tmp_path,
        SQLiteStorage(tmp_path / "runs.sqlite3"),
        adapters={"fake": lambda trial: RecordingAdapter([], adapter_result)},
        workspace_factory=lambda repo, revision, path: RecordingWorkspace(repo, revision, path, []),
        evaluator=evaluator,
    )

    row = await runner.run_trial(_trial(tmp_path))

    assert row["status"] == "failed"
    assert row["success"] == 0
    assert row["exit_code"] == 7
    assert row["stdout"] == "partial output"
    assert row["stderr"] == "agent failed"
    assert row["error"].startswith("AdapterExecutionError:")
    assert not evaluator_called


@pytest.mark.asyncio
async def test_nonzero_command_adapter_cannot_be_washed_clean_by_evaluator(
    tmp_path: Path,
) -> None:
    trial = _trial(tmp_path, adapter="command").model_copy(
        update={
            "agent": AgentConfig(
                id="command",
                adapter="command",
                command=(sys.executable, "-c", "raise SystemExit(9)"),
            )
        }
    )
    runner = TrialRunner(
        tmp_path,
        SQLiteStorage(tmp_path / "runs.sqlite3"),
        workspace_factory=lambda repo, revision, path: RecordingWorkspace(repo, revision, path, []),
        evaluator=lambda *args: asyncio.sleep(0, result=EvaluationResult(True, 0, "", "", ())),
    )

    row = await runner.run_trial(trial)

    assert row["status"] == "failed"
    assert row["success"] == 0
    assert row["exit_code"] == 9
    assert row["error"].startswith("AdapterExecutionError:")


@pytest.mark.asyncio
async def test_empty_adapter_mapping_does_not_enable_defaults(tmp_path: Path) -> None:
    trial = _trial(tmp_path)
    runner = TrialRunner(
        tmp_path,
        SQLiteStorage(tmp_path / "runs.sqlite3"),
        adapters={},
        workspace_factory=lambda repo, revision, path: RecordingWorkspace(repo, revision, path, []),
    )

    row = await runner.run_trial(trial)

    assert row["status"] == "failed"
    assert "unsupported adapter" in row["error"]


@pytest.mark.asyncio
async def test_two_runners_recover_stale_attempt_only_once(tmp_path: Path) -> None:
    storage_path = tmp_path / "runs.sqlite3"
    trial = _trial(tmp_path)
    seed = SQLiteStorage(storage_path)
    seed.claim_trial(trial)
    with closing(sqlite3.connect(storage_path)) as connection, connection:
        connection.execute(
            "UPDATE trials SET heartbeat_at=? WHERE id=?",
            ("2000-01-01T00:00:00+00:00", trial.id),
        )
    old_events = tmp_path / ".agentablate" / "runs" / trial.id / "events.jsonl"
    old_events.parent.mkdir(parents=True)
    old_events.write_text('{"kind":"old"}\n')
    executions = 0

    class SlowAdapter:
        async def run(self, trial: TrialSpec, cwd: Path) -> AdapterResult:
            nonlocal executions
            executions += 1
            await asyncio.sleep(0.05)
            return AdapterResult(0, (AgentEvent("new", 1.0, {}),), "", "")

    async def evaluator(task: TaskSpec, path: Path, timeout: float) -> EvaluationResult:
        return EvaluationResult(True, 0, "", "", ())

    def make_runner() -> TrialRunner:
        return TrialRunner(
            tmp_path,
            SQLiteStorage(storage_path),
            adapters={"fake": lambda trial: SlowAdapter()},
            workspace_factory=lambda repo, revision, path: RecordingWorkspace(
                repo, revision, path, []
            ),
            evaluator=evaluator,
            recover_running=True,
            lease_timeout_seconds=1,
            heartbeat_interval_seconds=0.01,
        )

    rows = await asyncio.gather(make_runner().run_trial(trial), make_runner().run_trial(trial))

    events = [json.loads(line) for line in old_events.read_text().splitlines()]
    assert executions == 1
    assert {row["status"] for row in rows} <= {"running", "completed"}
    assert [event["kind"] for event in events] == ["new"]


@pytest.mark.asyncio
async def test_runner_heartbeats_active_attempt(tmp_path: Path) -> None:
    started = asyncio.Event()

    class BlockingAdapter:
        async def run(self, trial: TrialSpec, cwd: Path) -> AdapterResult:
            started.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    storage = SQLiteStorage(tmp_path / "runs.sqlite3")
    trial = _trial(tmp_path)
    runner = TrialRunner(
        tmp_path,
        storage,
        adapters={"fake": lambda trial: BlockingAdapter()},
        workspace_factory=lambda repo, revision, path: RecordingWorkspace(repo, revision, path, []),
        heartbeat_interval_seconds=0.01,
    )
    running = asyncio.create_task(runner.run_trial(trial))
    await started.wait()
    initial = storage.get_trial(trial.id)["heartbeat_at"]
    await asyncio.sleep(0.04)

    assert storage.get_trial(trial.id)["heartbeat_at"] > initial
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running


@pytest.mark.asyncio
async def test_lost_lease_terminates_command_and_fails_trial(tmp_path: Path) -> None:
    leaked = tmp_path / "lease-lost-leaked"
    script = (
        f"import pathlib, time; time.sleep(0.5); pathlib.Path({str(leaked)!r}).write_text('leaked')"
    )
    base = _trial(tmp_path, adapter="command")
    trial = base.model_copy(
        update={
            "agent": AgentConfig(
                id="command",
                adapter="command",
                command=(sys.executable, "-c", script),
            )
        }
    )

    class LeaseLostStorage(SQLiteStorage):
        def heartbeat(self, trial_id: str, attempt_id: str) -> bool:
            return False

    calls: list[str] = []
    storage = LeaseLostStorage(tmp_path / "runs.sqlite3")
    runner = TrialRunner(
        tmp_path,
        storage,
        workspace_factory=lambda repo, revision, path: RecordingWorkspace(
            repo, revision, path, calls
        ),
        heartbeat_interval_seconds=0.01,
    )

    row = await runner.run_trial(trial)
    await asyncio.sleep(0.7)

    assert row["status"] == "failed"
    assert row["error"].startswith("TrialLeaseLost:")
    assert not leaked.exists()
    assert calls[-1] == "cleanup"


@pytest.mark.asyncio
async def test_heartbeat_error_cancels_custom_adapter_and_fails_trial(
    tmp_path: Path,
) -> None:
    leaked = tmp_path / "heartbeat-error-leaked"

    class HeartbeatFailingStorage(SQLiteStorage):
        def heartbeat(self, trial_id: str, attempt_id: str) -> bool:
            raise sqlite3.OperationalError("heartbeat unavailable")

    class DelayedAdapter:
        async def run(self, trial: TrialSpec, cwd: Path) -> AdapterResult:
            await asyncio.sleep(0.5)
            leaked.write_text("leaked")
            return AdapterResult(0, (), "", "")

    calls: list[str] = []
    storage = HeartbeatFailingStorage(tmp_path / "runs.sqlite3")
    runner = TrialRunner(
        tmp_path,
        storage,
        adapters={"fake": lambda trial: DelayedAdapter()},
        workspace_factory=lambda repo, revision, path: RecordingWorkspace(
            repo, revision, path, calls
        ),
        heartbeat_interval_seconds=0.01,
    )

    row = await runner.run_trial(_trial(tmp_path))
    await asyncio.sleep(0.7)

    assert row["status"] == "failed"
    assert row["error"].startswith("TrialLeaseLost:")
    assert "heartbeat unavailable" in row["error"]
    assert not leaked.exists()
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
        workspace_factory=lambda repo, revision, path: RecordingWorkspace(repo, revision, path, []),
    )

    row = await runner.run_trial(trial)

    assert row["status"] == "failed"
    assert "requires agent.command" in row["error"]
