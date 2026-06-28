from pathlib import Path

from agentablate.adapters.base import AdapterResult, AgentEvent
from agentablate.models import TrialSpec


class FakeAdapter:
    async def doctor(self) -> tuple[bool, str]:
        return True, "fake adapter is ready"

    async def run(self, trial: TrialSpec, cwd: Path) -> AdapterResult:
        output = f"{trial.id}\n"
        (cwd / "agentablate-output.txt").write_text(output)
        events = (
            AgentEvent("start", 0.0, {"trial_id": trial.id}),
            AgentEvent("message", 1.0, {"text": output.strip()}),
            AgentEvent("completed", 2.0, {"exit_code": 0}),
        )
        return AdapterResult(0, events, output, "")
