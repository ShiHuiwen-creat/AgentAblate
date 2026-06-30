import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from agentablate.models import TrialSpec


@dataclass(frozen=True)
class AgentEvent:
    kind: str
    timestamp: float
    data: dict[str, object]


@dataclass(frozen=True)
class AdapterResult:
    exit_code: int
    events: tuple[AgentEvent, ...]
    stdout: str
    stderr: str


class AdapterTimeout(TimeoutError):
    def __init__(self, result: AdapterResult) -> None:
        super().__init__("agent command timed out")
        self.result = result


class AdapterCancelled(asyncio.CancelledError):
    def __init__(self, result: AdapterResult) -> None:
        super().__init__("agent command cancelled")
        self.result = result


class AgentAdapter(Protocol):
    async def doctor(self) -> tuple[bool, str]: ...

    async def run(self, trial: TrialSpec, cwd: Path) -> AdapterResult: ...
