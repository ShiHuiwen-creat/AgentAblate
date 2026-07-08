import asyncio
import inspect
import json
import time
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from agentablate.adapters.base import (
    AdapterCancelled,
    AdapterResult,
    AdapterTimeout,
    AgentAdapter,
    AgentEvent,
)
from agentablate.adapters.codex_exec import CodexExecAdapter
from agentablate.adapters.command import CommandAdapter
from agentablate.adapters.fake import FakeAdapter
from agentablate.evaluators import (
    EvaluationCancelled,
    EvaluationResult,
    EvaluationTimeout,
    evaluate,
)
from agentablate.models import TrialSpec
from agentablate.redaction import Redactor
from agentablate.storage import SQLiteStorage
from agentablate.workspace import WorktreeWorkspace

AdapterFactory = Callable[[TrialSpec], AgentAdapter]
WorkspaceFactory = Callable[[Path, str, Path], Any]
Evaluator = Callable[..., Any]


class AdapterConfigurationError(ValueError):
    """Raised when an adapter lacks required trial configuration."""


class AdapterExecutionError(RuntimeError):
    """Raised when an adapter process exits unsuccessfully."""

    def __init__(self, result: AdapterResult) -> None:
        super().__init__(f"agent command exited with code {result.exit_code}")
        self.result = result


class TrialLeaseLost(RuntimeError):
    """Raised when a running trial no longer owns its persistence lease."""


class TrialRunner:
    def __init__(
        self,
        root: Path,
        storage: SQLiteStorage,
        *,
        adapters: dict[str, AdapterFactory] | None = None,
        workspace_factory: WorkspaceFactory = WorktreeWorkspace,
        evaluator: Evaluator = evaluate,
        recover_running: bool = False,
        lease_timeout_seconds: float = 60,
        heartbeat_interval_seconds: float = 10,
    ) -> None:
        if lease_timeout_seconds <= 0:
            raise ValueError("lease timeout must be positive")
        if heartbeat_interval_seconds <= 0:
            raise ValueError("heartbeat interval must be positive")
        if heartbeat_interval_seconds >= lease_timeout_seconds:
            raise ValueError("heartbeat interval must be shorter than lease timeout")
        self.root = root
        self.storage = storage
        self.workspace_factory = workspace_factory
        self.evaluator = evaluator
        self.recover_running = recover_running
        self.lease_timeout_seconds = lease_timeout_seconds
        self.heartbeat_interval_seconds = heartbeat_interval_seconds
        self._redactor = Redactor()
        self._recovery_attempted: set[str] = set()
        self.adapters = (
            adapters
            if adapters is not None
            else {
                "fake": lambda trial: FakeAdapter(),
                "command": self._command_adapter,
                "codex-exec": self._codex_exec_adapter,
            }
        )

    @staticmethod
    def _command_adapter(trial: TrialSpec) -> AgentAdapter:
        if not trial.agent.command:
            raise AdapterConfigurationError("command adapter requires agent.command")
        return CommandAdapter(trial.agent.command)

    @staticmethod
    def _codex_exec_adapter(trial: TrialSpec) -> AgentAdapter:
        if trial.adapter_runtime is None:
            raise AdapterConfigurationError("codex-exec adapter requires adapter_runtime")
        return CodexExecAdapter(trial.adapter_runtime)

    def events_path(self, trial_id: str) -> Path:
        if not trial_id or Path(trial_id).name != trial_id or trial_id in {".", ".."}:
            raise ValueError("trial id must be a single path-safe name")
        return self.root / ".agentablate" / "runs" / trial_id / "events.jsonl"

    def _append_events(self, trial_id: str, events: tuple[AgentEvent, ...]) -> None:
        if not events:
            return
        path = self.events_path(trial_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as stream:
            for event in events:
                record = {
                    "data": self._redactor.data(event.data),
                    "kind": event.kind,
                    "timestamp": event.timestamp,
                    "trial_id": trial_id,
                }
                line = json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
                stream.write(line)
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
        events_path = self.events_path(trial.id)
        self.storage.register_experiment(
            trial.experiment, trial.config_hash, f"{trial.experiment}.yml"
        )
        if self.recover_running and trial.id not in self._recovery_attempted:
            stale_before = datetime.now(UTC) - timedelta(seconds=self.lease_timeout_seconds)
            self.storage.recover_running(trial.id, stale_before=stale_before.isoformat())
            self._recovery_attempted.add(trial.id)
        claim = self.storage.claim_trial(trial)
        if claim.status != "claimed":
            row = self.storage.get_trial(trial.id)
            assert row is not None
            return row
        assert claim.attempt_id is not None
        attempt_id = claim.attempt_id
        heartbeat_task = asyncio.create_task(self._heartbeat(trial.id, attempt_id))
        started = time.monotonic()
        status = "failed"
        success: bool | None = False
        exit_code: int | None = None
        error_text: str | None = None
        stdout_parts: list[str] = []
        stderr_parts: list[str] = []
        exit_codes: list[int] = []
        cancelled: asyncio.CancelledError | None = None
        trial_failure: Exception | None = None

        try:
            events_path.unlink(missing_ok=True)
            execution_task = asyncio.create_task(
                self._execute_trial(trial, stdout_parts, stderr_parts, exit_codes)
            )
            try:
                done, _ = await asyncio.wait(
                    (execution_task, heartbeat_task),
                    return_when=asyncio.FIRST_COMPLETED,
                )
            except asyncio.CancelledError as external_cancel:
                cleanup_error = await self._cancel_execution(execution_task)
                if isinstance(cleanup_error, (AdapterCancelled, EvaluationCancelled)):
                    external_cancel.__cause__ = cleanup_error
                elif cleanup_error is not None:
                    external_cancel.add_note(f"trial cleanup failed: {cleanup_error}")
                raise
            if heartbeat_task in done:
                heartbeat = heartbeat_task
                heartbeat_task = None
                try:
                    await heartbeat
                except TrialLeaseLost as lease_error:
                    cleanup_error = await self._cancel_execution(execution_task)
                    if cleanup_error is not None:
                        lease_error.add_note(f"trial cleanup failed: {cleanup_error}")
                    raise
                raise AssertionError("heartbeat task stopped without losing its lease")
            success, exit_code = await execution_task
            status = "completed"
        except asyncio.CancelledError as exc:
            cancelled = exc
            persisted_error = (
                exc.__cause__
                if isinstance(exc.__cause__, (AdapterCancelled, EvaluationCancelled))
                else exc
            )
            error_text = self._redact_error(trial, persisted_error)
        except Exception as exc:
            trial_failure = exc
            error_text = self._redact_error(trial, exc)
        finally:
            if exit_codes:
                exit_code = exit_codes[-1]
            heartbeat_error: Exception | None = None
            if heartbeat_task is not None:
                try:
                    await self._stop_heartbeat(heartbeat_task)
                except Exception as error:
                    heartbeat_error = error
            if heartbeat_error is not None:
                heartbeat_detail = f"heartbeat failed: {heartbeat_error}"
                error_text = f"{error_text}; {heartbeat_detail}" if error_text else heartbeat_detail
                status = "failed"
                success = False
                if cancelled is not None:
                    cancelled.add_note(heartbeat_detail)
            try:
                finished = self.storage.finish_trial(
                    trial,
                    attempt_id,
                    status=status,
                    success=success,
                    duration_seconds=time.monotonic() - started,
                    exit_code=exit_code,
                    error=(
                        self._redact_text(trial, error_text) if error_text is not None else None
                    ),
                    stdout=self._redactor.text("\n".join(part for part in stdout_parts if part)),
                    stderr=self._redactor.text("\n".join(part for part in stderr_parts if part)),
                )
                if not finished:
                    ownership_error = RuntimeError("trial attempt ownership was lost before finish")
                    if trial_failure is not None:
                        trial_failure.add_note(str(ownership_error))
                        raise trial_failure
                    raise ownership_error
            except Exception as finish_error:
                if cancelled is None:
                    raise
                cancelled.add_note(f"failed to persist cancelled trial: {finish_error}")

        if cancelled is not None:
            raise cancelled
        row = self.storage.get_trial(trial.id)
        assert row is not None
        return row

    async def _execute_trial(
        self,
        trial: TrialSpec,
        stdout_parts: list[str],
        stderr_parts: list[str],
        exit_codes: list[int],
    ) -> tuple[bool, int]:
        workspace_path = self.root / ".agentablate" / "worktrees" / trial.id
        async with self.workspace_factory(
            trial.task.repo, trial.task.revision, workspace_path
        ) as workspace:
            adapter = self._adapter_for(trial)
            incremental = self._supports_event_sink(adapter)
            try:
                if incremental:
                    adapter_result = await adapter.run(
                        trial,
                        workspace.path,
                        on_event=lambda event: self._append_events(trial.id, (event,)),
                    )
                else:
                    adapter_result = await adapter.run(trial, workspace.path)
            except AdapterCancelled as exc:
                self._record_adapter_result(
                    trial.id,
                    exc.result,
                    stdout_parts,
                    stderr_parts,
                    include_events=not incremental,
                )
                exit_codes.append(exc.result.exit_code)
                raise
            except AdapterTimeout as exc:
                self._record_adapter_result(
                    trial.id,
                    exc.result,
                    stdout_parts,
                    stderr_parts,
                    include_events=not incremental,
                )
                exit_codes.append(exc.result.exit_code)
                raise
            self._record_adapter_result(
                trial.id,
                adapter_result,
                stdout_parts,
                stderr_parts,
                include_events=not incremental,
            )
            exit_codes.append(adapter_result.exit_code)
            if adapter_result.exit_code != 0:
                raise AdapterExecutionError(adapter_result)
            try:
                evaluation: EvaluationResult = await self.evaluator(
                    trial.task, workspace.path, trial.timeout_seconds
                )
            except EvaluationCancelled as exc:
                stdout_parts.append(exc.result.stdout)
                stderr_parts.append(exc.result.stderr)
                exit_codes.append(exc.result.exit_code)
                raise
            except EvaluationTimeout as exc:
                stdout_parts.append(exc.result.stdout)
                stderr_parts.append(exc.result.stderr)
                exit_codes.append(exc.result.exit_code)
                raise
            stdout_parts.append(evaluation.stdout)
            stderr_parts.append(evaluation.stderr)
            exit_codes.append(evaluation.exit_code)
            return evaluation.success, evaluation.exit_code

    async def _heartbeat(self, trial_id: str, attempt_id: str) -> None:
        while True:
            await asyncio.sleep(self.heartbeat_interval_seconds)
            try:
                owned = self.storage.heartbeat(trial_id, attempt_id)
            except Exception as error:
                raise TrialLeaseLost(f"trial heartbeat failed: {error}") from error
            if not owned:
                raise TrialLeaseLost("trial lease ownership was lost")

    @staticmethod
    async def _cancel_execution(
        task: asyncio.Task[tuple[bool, int]],
    ) -> BaseException | None:
        task.cancel()
        try:
            await task
        except (AdapterCancelled, EvaluationCancelled) as error:
            return error
        except asyncio.CancelledError:
            return None
        except BaseException as error:
            return error
        return None

    @staticmethod
    async def _stop_heartbeat(task: asyncio.Task[None]) -> None:
        task.cancel()
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            if not task.cancelled():
                raise

    def _record_adapter_result(
        self,
        trial_id: str,
        result: AdapterResult,
        stdout_parts: list[str],
        stderr_parts: list[str],
        *,
        include_events: bool,
    ) -> None:
        if include_events:
            self._append_events(trial_id, result.events)
        stdout_parts.append(result.stdout)
        stderr_parts.append(result.stderr)

    @staticmethod
    def _supports_event_sink(adapter: AgentAdapter) -> bool:
        parameters = inspect.signature(adapter.run).parameters.values()
        return any(
            parameter.name == "on_event" or parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in parameters
        )

    def _redact_error(self, trial: TrialSpec, error: BaseException) -> str:
        return self._redact_text(trial, f"{type(error).__name__}: {error}")

    def _redact_text(self, trial: TrialSpec, text: str) -> str:
        replacements = sorted(
            ((str(self.root.resolve()), "<root>"), (str(trial.task.repo.resolve()), "<repo>")),
            key=lambda item: len(item[0]),
            reverse=True,
        )
        for value, replacement in replacements:
            text = text.replace(value, replacement)
        return self._redactor.text(text)

    async def run_all(self, trials: Iterable[TrialSpec], concurrency: int) -> list[dict[str, Any]]:
        if concurrency < 1:
            raise ValueError("concurrency must be at least 1")
        semaphore = asyncio.Semaphore(concurrency)

        async def limited(trial: TrialSpec) -> dict[str, Any]:
            async with semaphore:
                try:
                    return await self.run_trial(trial)
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    return {
                        "id": trial.id,
                        "status": "failed",
                        "success": False,
                        "error": self._redact_error(trial, error),
                    }

        return list(await asyncio.gather(*(limited(trial) for trial in trials)))
