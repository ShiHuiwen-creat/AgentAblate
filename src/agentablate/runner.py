import asyncio
import json
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from agentablate.adapters.base import (
    AdapterCancelled,
    AdapterResult,
    AdapterTimeout,
    AgentAdapter,
    AgentEvent,
)
from agentablate.adapters.command import CommandAdapter
from agentablate.adapters.fake import FakeAdapter
from agentablate.evaluators import EvaluationResult, EvaluationTimeout, evaluate
from agentablate.models import TrialSpec
from agentablate.storage import SQLiteStorage
from agentablate.workspace import WorktreeWorkspace

AdapterFactory = Callable[[TrialSpec], AgentAdapter]
WorkspaceFactory = Callable[[Path, str, Path], Any]
Evaluator = Callable[..., Any]


class AdapterConfigurationError(ValueError):
    """Raised when an adapter lacks required trial configuration."""


class TrialRunner:
    def __init__(
        self,
        root: Path,
        storage: SQLiteStorage,
        *,
        adapters: dict[str, AdapterFactory] | None = None,
        workspace_factory: WorkspaceFactory = WorktreeWorkspace,
        evaluator: Evaluator = evaluate,
    ) -> None:
        self.root = root
        self.storage = storage
        self.workspace_factory = workspace_factory
        self.evaluator = evaluator
        self.adapters = adapters or {
            "fake": lambda trial: FakeAdapter(),
            "command": self._command_adapter,
        }

    @staticmethod
    def _command_adapter(trial: TrialSpec) -> AgentAdapter:
        if not trial.agent.command:
            raise AdapterConfigurationError("command adapter requires agent.command")
        return CommandAdapter(trial.agent.command)

    def events_path(self, trial_id: str) -> Path:
        return self.root / ".agentablate" / "runs" / trial_id / "events.jsonl"

    def _append_events(self, trial_id: str, events: tuple[AgentEvent, ...]) -> None:
        if not events:
            return
        path = self.events_path(trial_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as stream:
            for event in events:
                record = {
                    "data": event.data,
                    "kind": event.kind,
                    "timestamp": event.timestamp,
                    "trial_id": trial_id,
                }
                stream.write(json.dumps(record, sort_keys=True, separators=(",", ":")))
                stream.write("\n")
                stream.flush()

    def _adapter_for(self, trial: TrialSpec) -> AgentAdapter:
        try:
            factory = self.adapters[trial.agent.adapter]
        except KeyError as error:
            raise AdapterConfigurationError(
                f"unsupported adapter: {trial.agent.adapter}"
            ) from error
        return factory(trial)

    async def run_trial(self, trial: TrialSpec) -> dict[str, Any]:
        self.storage.register_experiment(
            trial.experiment, trial.config_hash, f"{trial.experiment}.yml"
        )
        if self.storage.is_completed(trial.id):
            row = self.storage.get_trial(trial.id)
            assert row is not None
            return row

        self.storage.start_trial(trial)
        started = time.monotonic()
        status = "failed"
        success: bool | None = False
        exit_code: int | None = None
        error_text: str | None = None
        stdout_parts: list[str] = []
        stderr_parts: list[str] = []
        cancelled: asyncio.CancelledError | None = None

        try:
            workspace_path = self.root / ".agentablate" / "worktrees" / trial.id
            async with self.workspace_factory(
                trial.task.repo, trial.task.revision, workspace_path
            ) as workspace:
                adapter = self._adapter_for(trial)
                try:
                    adapter_result = await adapter.run(trial, workspace.path)
                except AdapterCancelled as exc:
                    self._record_adapter_result(
                        trial.id, exc.result, stdout_parts, stderr_parts
                    )
                    exit_code = exc.result.exit_code
                    raise
                except AdapterTimeout as exc:
                    self._record_adapter_result(trial.id, exc.result, stdout_parts, stderr_parts)
                    exit_code = exc.result.exit_code
                    raise
                self._record_adapter_result(
                    trial.id, adapter_result, stdout_parts, stderr_parts
                )
                exit_code = adapter_result.exit_code
                try:
                    evaluation: EvaluationResult = await self.evaluator(
                        trial.task, workspace.path, trial.timeout_seconds
                    )
                except EvaluationTimeout as exc:
                    stdout_parts.append(exc.result.stdout)
                    stderr_parts.append(exc.result.stderr)
                    exit_code = exc.result.exit_code
                    raise
                stdout_parts.append(evaluation.stdout)
                stderr_parts.append(evaluation.stderr)
                exit_code = evaluation.exit_code
                success = evaluation.success
                status = "completed"
        except asyncio.CancelledError as exc:
            cancelled = exc
            error_text = f"{type(exc).__name__}: {exc}"
        except Exception as exc:
            error_text = f"{type(exc).__name__}: {exc}"
        finally:
            self.storage.finish_trial(
                trial,
                status=status,
                success=success,
                duration_seconds=time.monotonic() - started,
                exit_code=exit_code,
                error=error_text,
                stdout="\n".join(part for part in stdout_parts if part),
                stderr="\n".join(part for part in stderr_parts if part),
            )

        if cancelled is not None:
            raise cancelled
        row = self.storage.get_trial(trial.id)
        assert row is not None
        return row

    def _record_adapter_result(
        self,
        trial_id: str,
        result: AdapterResult,
        stdout_parts: list[str],
        stderr_parts: list[str],
    ) -> None:
        self._append_events(trial_id, result.events)
        stdout_parts.append(result.stdout)
        stderr_parts.append(result.stderr)

    async def run_all(
        self, trials: Iterable[TrialSpec], concurrency: int
    ) -> list[dict[str, Any]]:
        if concurrency < 1:
            raise ValueError("concurrency must be at least 1")
        semaphore = asyncio.Semaphore(concurrency)

        async def limited(trial: TrialSpec) -> dict[str, Any]:
            async with semaphore:
                return await self.run_trial(trial)

        return list(await asyncio.gather(*(limited(trial) for trial in trials)))
