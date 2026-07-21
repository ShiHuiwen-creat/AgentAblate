import asyncio
import os
import shutil
import sys
import time
from pathlib import Path

from agentablate.adapters.base import (
    AdapterCancelled,
    AdapterResult,
    AdapterTimeout,
    AgentEvent,
    EventSink,
)
from agentablate.models import TrialSpec
from agentablate.processes import (
    ProcessCancelled,
    ProcessTimeout,
    communicate,
    create_process,
    minimal_environment,
    terminate_process,
)


class CommandAdapter:
    def __init__(
        self,
        command: tuple[str, ...],
        *,
        allowed_env: tuple[str, ...] = (),
        interpolate_prompt: bool = True,
    ) -> None:
        if not command:
            raise ValueError("command must not be empty")
        self.command = command
        self.allowed_env = allowed_env
        self.interpolate_prompt = interpolate_prompt

    async def doctor(self) -> tuple[bool, str]:
        executable = sys.executable if self.command[0] == "{python}" else self.command[0]
        has_path = os.sep in executable or bool(os.altsep and os.altsep in executable)
        if has_path:
            path = Path(executable)
            available = path.is_absolute() and path.is_file() and os.access(path, os.X_OK)
        else:
            available = shutil.which(executable) is not None
        message = f"{executable} is {'available' if available else 'missing'}"
        return available, message

    async def run(
        self, trial: TrialSpec, cwd: Path, *, on_event: EventSink | None = None
    ) -> AdapterResult:
        command = tuple(
            sys.executable
            if argument == "{python}"
            else (
                argument.replace("{prompt}", trial.task.prompt)
                if self.interpolate_prompt
                else argument
            )
            for argument in self.command
        )
        environment = minimal_environment(self.allowed_env)
        started = time.monotonic()
        start_event = AgentEvent("start", started, {"executable": Path(command[0]).name})
        process = await create_process(
            command,
            cwd=cwd,
            env=environment,
        )
        try:
            return await self._run_process(process, trial, start_event, on_event)
        except BaseException as error:
            try:
                await terminate_process(process)
            except BaseException as cleanup_error:
                error.add_note(f"process cleanup failed: {cleanup_error}")
            raise

    async def _run_process(
        self,
        process: asyncio.subprocess.Process,
        trial: TrialSpec,
        start_event: AgentEvent,
        on_event: EventSink | None,
    ) -> AdapterResult:
        if on_event is not None:
            on_event(start_event)
        try:
            stdout_bytes, stderr_bytes = await communicate(process, trial.timeout_seconds)
        except ProcessCancelled as error:
            events = (
                start_event,
                AgentEvent("cancelled", time.monotonic(), {"exit_code": error.exit_code}),
            )
            sink_error = self._emit_terminal(on_event, events[-1])
            result = AdapterResult(
                error.exit_code,
                events,
                error.stdout.decode(errors="replace"),
                self._stderr_with_sink_error(error.stderr, sink_error),
            )
            cancelled = AdapterCancelled(result)
            if sink_error is not None:
                cancelled.add_note(str(sink_error))
            raise cancelled from error
        except ProcessTimeout as error:
            events = (
                start_event,
                AgentEvent("timeout", time.monotonic(), {"exit_code": error.exit_code}),
            )
            sink_error = self._emit_terminal(on_event, events[-1])
            result = AdapterResult(
                process.returncode if process.returncode is not None else -1,
                events,
                error.stdout.decode(errors="replace"),
                self._stderr_with_sink_error(error.stderr, sink_error),
            )
            timeout = AdapterTimeout(result)
            if sink_error is not None:
                timeout.add_note(str(sink_error))
            raise timeout from error

        events = (
            start_event,
            AgentEvent(
                "completed",
                time.monotonic(),
                {"exit_code": process.returncode},
            ),
        )
        if on_event is not None:
            on_event(events[-1])
        return AdapterResult(
            process.returncode if process.returncode is not None else 0,
            events,
            stdout_bytes.decode(errors="replace"),
            stderr_bytes.decode(errors="replace"),
        )

    @staticmethod
    def _emit_terminal(on_event: EventSink | None, event: AgentEvent) -> BaseException | None:
        if on_event is None:
            return None
        try:
            on_event(event)
        except BaseException as error:
            return error
        return None

    @staticmethod
    def _stderr_with_sink_error(stderr: bytes, error: BaseException | None) -> str:
        text = stderr.decode(errors="replace")
        if error is None:
            return text
        suffix = f"event sink failed: {error}"
        return f"{text.rstrip()}\n{suffix}" if text else suffix
