import asyncio
import os
import shutil
import sqlite3
import sys
import tempfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from agentablate.adapters.base import AgentAdapter
from agentablate.adapters.codex_exec import CodexExecAdapter, discover_codex_runtime
from agentablate.adapters.command import CommandAdapter
from agentablate.adapters.fake import FakeAdapter
from agentablate.config import load_experiment
from agentablate.matrix import expand_matrix
from agentablate.models import AgentConfig, ExperimentBundle, TrialSpec
from agentablate.reporting import (
    Comparison,
    load_comparison,
    load_report,
    render_html,
    render_markdown,
)
from agentablate.runner import TrialRunner
from agentablate.storage import SQLiteStorage
from agentablate.workspace import WorkspaceError, initialize_fixture_repository

Loader = Callable[[Path], ExperimentBundle]
MatrixExpander = Callable[[ExperimentBundle], list[TrialSpec]]


class ApplicationError(RuntimeError):
    """Expected user-facing workflow failure."""


@dataclass(frozen=True, slots=True)
class DoctorResult:
    agent_id: str
    available: bool
    detail: str


@dataclass(frozen=True, slots=True)
class RunSummary:
    total: int
    failed: int


def _adapter(agent: AgentConfig) -> AgentAdapter:
    if agent.adapter == "fake":
        return FakeAdapter()
    if agent.adapter == "command":
        return CommandAdapter(agent.command or ())
    if agent.adapter == "codex-exec":
        return CodexExecAdapter(discover_codex_runtime())
    raise ApplicationError(f"adapter is not available in Phase 1: {agent.adapter}")


async def doctor_experiment_async(
    config: Path,
    *,
    loader: Loader = load_experiment,
    adapter_factory: Callable[[AgentConfig], AgentAdapter] = _adapter,
) -> tuple[DoctorResult, ...]:
    try:
        bundle = loader(config)

        results = []
        for agent in bundle.config.agents:
            available, detail = await adapter_factory(agent).doctor()
            results.append(DoctorResult(agent.id, available, detail))
        return tuple(results)
    except ApplicationError:
        raise
    except (OSError, ValueError, WorkspaceError, RuntimeError) as error:
        raise ApplicationError(str(error)) from error


def _require_sync_context() -> None:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    raise ApplicationError(
        "synchronous workflow cannot run inside a running event loop; use the async API"
    )


def doctor_experiment(
    config: Path,
    *,
    loader: Loader = load_experiment,
    adapter_factory: Callable[[AgentConfig], AgentAdapter] = _adapter,
) -> tuple[DoctorResult, ...]:
    _require_sync_context()
    return asyncio.run(
        doctor_experiment_async(config, loader=loader, adapter_factory=adapter_factory)
    )


async def run_experiment_async(
    config: Path,
    *,
    agents: Iterable[str] = (),
    variants: Iterable[str] = (),
    tasks: Iterable[str] = (),
    concurrency: int = 1,
    resume: bool = True,
    loader: Loader = load_experiment,
    matrix_expander: MatrixExpander = expand_matrix,
    runner_factory: Callable[..., Any] = TrialRunner,
    allow_empty: bool = False,
) -> RunSummary:
    try:
        bundle = loader(config)
        selected_agents = set(agents)
        selected_variants = set(variants)
        selected_tasks = set(tasks)
        trials = [
            trial
            for trial in matrix_expander(bundle)
            if (not selected_agents or trial.agent.id in selected_agents)
            and (not selected_variants or trial.variant.id in selected_variants)
            and (not selected_tasks or trial.task.id in selected_tasks)
        ]
        if not trials and not allow_empty:
            raise ApplicationError("No trials match the selected filters.")
        root = bundle.source.parent
        storage = SQLiteStorage(root / ".agentablate" / "results.sqlite3")
        runner = runner_factory(root, storage, recover_running=resume)
        rows = await runner.run_all(trials, concurrency)
        return RunSummary(len(rows), sum(row.get("status") != "completed" for row in rows))
    except ApplicationError:
        raise
    except (OSError, ValueError, sqlite3.Error, WorkspaceError, RuntimeError) as error:
        raise ApplicationError(str(error)) from error


def run_experiment(
    config: Path,
    *,
    agents: Iterable[str] = (),
    variants: Iterable[str] = (),
    tasks: Iterable[str] = (),
    concurrency: int = 1,
    resume: bool = True,
    loader: Loader = load_experiment,
    matrix_expander: MatrixExpander = expand_matrix,
    runner_factory: Callable[..., Any] = TrialRunner,
    allow_empty: bool = False,
) -> RunSummary:
    _require_sync_context()
    return asyncio.run(
        run_experiment_async(
            config,
            agents=agents,
            variants=variants,
            tasks=tasks,
            concurrency=concurrency,
            resume=resume,
            loader=loader,
            matrix_expander=matrix_expander,
            runner_factory=runner_factory,
            allow_empty=allow_empty,
        )
    )


def compare_results(database: Path) -> Comparison:
    try:
        return load_comparison(database)
    except (OSError, sqlite3.Error, RuntimeError) as error:
        raise ApplicationError(str(error)) from error


def write_report(database: Path, destination: Path, format: str) -> None:
    try:
        report = load_report(database)
        rendered = render_markdown(report) if format == "markdown" else render_html(report)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(rendered, encoding="utf-8")
    except (OSError, sqlite3.Error, RuntimeError) as error:
        raise ApplicationError(str(error)) from error


def initialize_experiment(directory: Path, config_text: str, task_text: str, force: bool) -> None:
    targets = (directory / "agentablate.yaml", directory / "task.yaml", directory / "fixture")
    existing = [path.name for path in targets if path.exists()]
    if existing and not force:
        raise ApplicationError(f"Refusing to overwrite; already exists: {', '.join(existing)}")
    staging: Path | None = None
    try:
        directory.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".agentablate-init-", dir=directory))
        payload, backup = staging / "payload", staging / "backup"
        payload.mkdir()
        backup.mkdir()
        initialize_fixture_repository(payload / "fixture")
        (payload / "agentablate.yaml").write_text(config_text, encoding="utf-8")
        (payload / "task.yaml").write_text(task_text, encoding="utf-8")
        moved: list[Path] = []
        installed: list[Path] = []
        try:
            for target in targets:
                if target.exists():
                    os.replace(target, backup / target.name)
                    moved.append(target)
            for target in targets:
                (payload / target.name).replace(target)
                installed.append(target)
        except OSError:
            for target in reversed(installed):
                if target.is_dir():
                    shutil.rmtree(target)
                elif target.exists():
                    target.unlink()
            for target in reversed(moved):
                os.replace(backup / target.name, target)
            raise
    except (OSError, WorkspaceError) as error:
        raise ApplicationError(f"Initialization failed: {error}") from error
    finally:
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)


def starter_documents(python_executable: str | None = None) -> tuple[str, str]:
    executable = python_executable or sys.executable
    config = {
        "version": 1,
        "experiment": {"name": "starter", "repetitions": 1, "timeout_seconds": 30},
        "agents": [{"id": "fake", "adapter": "fake"}],
        "variants": [{"id": "baseline"}],
        "tasks": ["./task.yaml"],
    }
    task = {
        "id": "starter-task",
        "repo": "./fixture",
        "revision": "HEAD",
        "prompt": "Create agentablate-output.txt.",
        "test_command": [
            executable,
            "-c",
            "import pathlib; assert pathlib.Path('agentablate-output.txt').is_file()",
        ],
    }

    class IndentedSafeDumper(yaml.SafeDumper):
        def increase_indent(self, flow: bool = False, indentless: bool = False):
            return super().increase_indent(flow, indentless=False)

    return (
        yaml.dump(config, Dumper=IndentedSafeDumper, sort_keys=False),
        yaml.dump(task, Dumper=IndentedSafeDumper, sort_keys=False),
    )
