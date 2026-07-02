from pathlib import Path

from agentablate.experiment import doctor_experiment, run_experiment
from agentablate.models import AgentConfig, ExperimentBundle, ExperimentConfig, ExperimentMeta


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
