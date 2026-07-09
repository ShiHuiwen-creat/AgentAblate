import asyncio
import json
import sys
import time
from pathlib import Path

import pytest

from agentablate.adapters.base import AdapterCancelled, AdapterTimeout, AgentEvent
from agentablate.adapters.command import CommandAdapter
from agentablate.adapters.fake import FakeAdapter
from agentablate.models import AgentConfig, TaskSpec, TrialSpec, VariantConfig


@pytest.fixture
def trial(tmp_path: Path) -> TrialSpec:
    return TrialSpec(
        id="trial-1",
        experiment="demo",
        agent=AgentConfig(id="fake", adapter="fake"),
        variant=VariantConfig(id="baseline"),
        task=TaskSpec(
            id="task-1",
            repo=tmp_path,
            prompt="hello; touch should-not-exist",
            test_command=("true",),
        ),
        repetition=0,
        timeout_seconds=1,
        config_hash="config-hash",
        extension_hashes=(),
    )


@pytest.mark.asyncio
async def test_fake_adapter_is_deterministic(
    trial: TrialSpec,
    tmp_path: Path,
) -> None:
    adapter = FakeAdapter()

    first = await adapter.run(trial, tmp_path)
    second = await adapter.run(trial, tmp_path)

    assert first == second
    assert [event.kind for event in first.events] == ["start", "message", "completed"]
    assert (tmp_path / "agentablate-output.txt").read_text() == "trial-1\n"


@pytest.mark.asyncio
async def test_command_adapter_substitutes_prompt_without_a_shell(
    trial: TrialSpec,
    tmp_path: Path,
) -> None:
    adapter = CommandAdapter((sys.executable, "-c", "import sys; print(sys.argv[1])", "{prompt}"))

    result = await adapter.run(trial, tmp_path)

    assert result.exit_code == 0
    assert result.stdout.strip() == trial.task.prompt
    assert result.events[0].data["executable"] == Path(sys.executable).name
    assert not (tmp_path / "should-not-exist").exists()


@pytest.mark.asyncio
async def test_command_adapter_can_disable_prompt_interpolation(
    trial: TrialSpec, tmp_path: Path
) -> None:
    literal = "literal {prompt}"
    adapter = CommandAdapter(
        (sys.executable, "-c", "import sys; print(sys.argv[1])", literal),
        interpolate_prompt=False,
    )

    result = await adapter.run(trial, tmp_path)

    assert result.stdout.strip() == literal


@pytest.mark.asyncio
async def test_command_adapter_filters_secrets(
    trial: TrialSpec,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SERVICE_TOKEN", "token-value")
    monkeypatch.setenv("SERVICE_KEY", "key-value")
    monkeypatch.setenv("SERVICE_SECRET", "secret-value")
    script = "import json, os; print(json.dumps(sorted(os.environ)))"

    filtered = await CommandAdapter((sys.executable, "-c", script)).run(trial, tmp_path)
    allowed = await CommandAdapter(
        (sys.executable, "-c", "import os; print(os.environ['SERVICE_TOKEN'])"),
        allowed_env=("SERVICE_TOKEN",),
    ).run(trial, tmp_path)

    child_keys = json.loads(filtered.stdout)
    assert "SERVICE_TOKEN" not in child_keys
    assert "SERVICE_KEY" not in child_keys
    assert "SERVICE_SECRET" not in child_keys
    assert allowed.stdout.strip() == "token-value"


@pytest.mark.asyncio
async def test_command_timeout_preserves_output_and_events(
    trial: TrialSpec,
    tmp_path: Path,
) -> None:
    script = "import time; print('started', flush=True); time.sleep(5)"
    adapter = CommandAdapter((sys.executable, "-c", script))

    with pytest.raises(AdapterTimeout) as captured:
        await adapter.run(trial, tmp_path)

    assert "started" in captured.value.result.stdout
    assert [event.kind for event in captured.value.result.events] == [
        "start",
        "timeout",
    ]


@pytest.mark.asyncio
async def test_cancelling_command_terminates_process(
    trial: TrialSpec,
    tmp_path: Path,
) -> None:
    marker = tmp_path / "leaked"
    script = (
        f"import pathlib, time; time.sleep(0.8); pathlib.Path({str(marker)!r}).write_text('leaked')"
    )
    running = asyncio.create_task(
        CommandAdapter((sys.executable, "-c", script)).run(trial, tmp_path)
    )
    await asyncio.sleep(0.1)

    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    await asyncio.sleep(0.9)

    assert not marker.exists()


@pytest.mark.asyncio
async def test_timeout_terminates_process_tree(
    trial: TrialSpec,
    tmp_path: Path,
) -> None:
    script = (
        "import subprocess, sys, time; "
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(5)']); "
        "print('started', flush=True); time.sleep(5)"
    )
    started = time.monotonic()

    with pytest.raises(AdapterTimeout):
        await CommandAdapter((sys.executable, "-c", script)).run(trial, tmp_path)

    assert time.monotonic() - started < 2.5


@pytest.mark.asyncio
async def test_doctor_rejects_non_executable_file(tmp_path: Path) -> None:
    command = tmp_path / "tool"
    command.write_text("not executable")

    available, _ = await CommandAdapter((str(command),)).doctor()

    assert not available


@pytest.mark.asyncio
async def test_start_event_sink_failure_terminates_process(
    trial: TrialSpec, tmp_path: Path
) -> None:
    leaked = tmp_path / "start-sink-leaked"
    script = (
        f"import pathlib, time; time.sleep(0.5); pathlib.Path({str(leaked)!r}).write_text('leaked')"
    )

    with pytest.raises(OSError, match="event write failed"):
        await CommandAdapter((sys.executable, "-c", script)).run(
            trial,
            tmp_path,
            on_event=lambda event: (_ for _ in ()).throw(OSError("event write failed")),
        )
    await asyncio.sleep(0.7)

    assert not leaked.exists()


@pytest.mark.asyncio
async def test_cancelled_event_sink_failure_preserves_cancellation_and_reaps(
    trial: TrialSpec, tmp_path: Path
) -> None:
    ready = tmp_path / "ready"
    leaked = tmp_path / "cancel-sink-leaked"
    script = (
        "import pathlib, time; "
        f"pathlib.Path({str(ready)!r}).write_text('ready'); "
        "print('partial', flush=True); time.sleep(0.5); "
        f"pathlib.Path({str(leaked)!r}).write_text('leaked')"
    )

    def sink(event: AgentEvent) -> None:
        if event.kind == "cancelled":
            raise OSError("terminal event write failed")

    running = asyncio.create_task(
        CommandAdapter((sys.executable, "-c", script)).run(trial, tmp_path, on_event=sink)
    )
    async with asyncio.timeout(2):
        while not ready.exists():
            await asyncio.sleep(0.01)
    running.cancel()

    with pytest.raises(asyncio.CancelledError) as captured:
        await running
    await asyncio.sleep(0.7)

    assert isinstance(captured.value, AdapterCancelled)
    assert "partial" in captured.value.result.stdout
    assert "terminal event write failed" in captured.value.result.stderr
    assert not leaked.exists()
