import sqlite3
from collections import Counter
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from agentablate import __version__
from agentablate.storage import SQLiteStorage


@dataclass(frozen=True, slots=True)
class ReportRow:
    agent_id: str
    variant_id: str
    task_ids: tuple[str, ...]
    trial_count: int
    success_count: int
    success_rate: float
    mean_duration: float
    failure_reasons: tuple[tuple[str, int], ...]


@dataclass(frozen=True, slots=True)
class Report:
    rows: tuple[ReportRow, ...]
    config_hashes: tuple[str, ...]
    repetitions: int
    has_baseline: bool
    adapter_implementations: tuple[tuple[str, str], ...] = ()
    version: str = __version__


@dataclass(frozen=True, slots=True)
class ComparisonRow:
    agent_id: str
    variant_id: str
    baseline_success_count: int
    baseline_trial_count: int
    variant_success_count: int
    variant_trial_count: int
    success_count_delta: int
    success_rate_delta: float


@dataclass(frozen=True, slots=True)
class Comparison:
    rows: tuple[ComparisonRow, ...]
    missing_baseline_agents: tuple[str, ...]


def _failure_category(error: str | None) -> str:
    if not error:
        return "unspecified failure"
    return error.split(":", 1)[0].strip()


def _prepare_database(database: Path) -> None:
    if not database.is_file():
        raise FileNotFoundError(f"results database does not exist: {database}")
    SQLiteStorage(database)


def _markdown_cell(value: object) -> str:
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace("|", "\\|")
        .replace("\r\n", "<br>")
        .replace("\r", "<br>")
        .replace("\n", "<br>")
    )


def load_report(database: Path) -> Report:
    """Read trial details and aggregate them without exposing stored source paths."""
    _prepare_database(database)
    with closing(sqlite3.connect(database)) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """SELECT t.agent_id, t.variant_id, t.task_id, t.repetition, t.success,
                      t.duration_seconds, t.error, t.config_hash,
                      t.adapter_type, t.implementation_version
               FROM trials AS t
               JOIN experiments AS e
                 ON e.name = t.experiment AND e.config_hash = t.config_hash
               WHERE t.status IN ('completed', 'failed')
               ORDER BY agent_id, variant_id, task_id, repetition, id"""
        ).fetchall()

    groups: dict[tuple[str, str], list[sqlite3.Row]] = {}
    for row in rows:
        groups.setdefault((row["agent_id"], row["variant_id"]), []).append(row)

    aggregates: list[ReportRow] = []
    for (agent_id, variant_id), trials in sorted(groups.items()):
        successes = sum(row["success"] == 1 for row in trials)
        durations = [
            row["duration_seconds"] for row in trials if row["duration_seconds"] is not None
        ]
        failures = Counter(_failure_category(row["error"]) for row in trials if row["success"] != 1)
        aggregates.append(
            ReportRow(
                agent_id=agent_id,
                variant_id=variant_id,
                task_ids=tuple(sorted({row["task_id"] for row in trials})),
                trial_count=len(trials),
                success_count=successes,
                success_rate=successes / len(trials),
                mean_duration=sum(durations) / len(durations) if durations else 0.0,
                failure_reasons=tuple(sorted(failures.items())),
            )
        )
    repetitions = max((row["repetition"] for row in rows), default=-1) + 1
    return Report(
        rows=tuple(aggregates),
        config_hashes=tuple(sorted({row["config_hash"] for row in rows})),
        repetitions=repetitions,
        has_baseline=any(row["variant_id"] == "baseline" for row in rows),
        adapter_implementations=tuple(
            sorted({(row["adapter_type"], row["implementation_version"]) for row in rows})
        ),
    )


def load_comparison(database: Path) -> Comparison:
    _prepare_database(database)
    with closing(sqlite3.connect(database)) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """SELECT t.experiment, t.config_hash, t.agent_id, t.variant_id,
                      t.task_id, t.repetition, t.success
               FROM trials AS t
               JOIN experiments AS e
                 ON e.name = t.experiment AND e.config_hash = t.config_hash
               WHERE t.status IN ('completed', 'failed')
               ORDER BY t.experiment, t.config_hash, t.agent_id, t.variant_id,
                        t.task_id, t.repetition, t.id"""
        ).fetchall()
    by_agent: dict[str, dict[str, dict[tuple[str, str, str, int], bool]]] = {}
    for row in rows:
        sample = (row["experiment"], row["config_hash"], row["task_id"], row["repetition"])
        by_agent.setdefault(row["agent_id"], {}).setdefault(row["variant_id"], {})[sample] = (
            row["success"] == 1
        )
    comparisons: list[ComparisonRow] = []
    missing: list[str] = []
    for agent_id, variants in sorted(by_agent.items()):
        baseline = variants.get("baseline")
        if baseline is None:
            missing.append(agent_id)
            continue
        for variant_id, variant in sorted(variants.items()):
            if variant_id == "baseline":
                continue
            common = sorted(baseline.keys() & variant.keys())
            if not common:
                continue
            baseline_count = sum(baseline[sample] for sample in common)
            variant_count = sum(variant[sample] for sample in common)
            trial_count = len(common)
            comparisons.append(
                ComparisonRow(
                    agent_id,
                    variant_id,
                    baseline_count,
                    trial_count,
                    variant_count,
                    trial_count,
                    variant_count - baseline_count,
                    (variant_count - baseline_count) / trial_count,
                )
            )
    return Comparison(tuple(comparisons), tuple(missing))


def render_comparison(comparison: Comparison) -> str:
    lines = [
        "| Agent | Variant | Baseline (baseline) success | Variant success | "
        "Count delta | Rate delta |",
        "|---|---|---:|---:|---:|---:|",
    ]
    for row in comparison.rows:
        baseline_rate = row.baseline_success_count / row.baseline_trial_count
        variant_rate = row.variant_success_count / row.variant_trial_count
        lines.append(
            f"| {_markdown_cell(row.agent_id)} | {_markdown_cell(row.variant_id)} | "
            f"{row.baseline_success_count}/{row.baseline_trial_count} ({baseline_rate:.1%}) | "
            f"{row.variant_success_count}/{row.variant_trial_count} ({variant_rate:.1%}) | "
            f"{row.success_count_delta:+d} | {row.success_rate_delta * 100:+.1f} pp |"
        )
    if not comparison.rows:
        lines.append("No baseline comparisons are available.")
    if comparison.missing_baseline_agents:
        lines.append(
            "No baseline variant for: "
            + ", ".join(_markdown_cell(item) for item in comparison.missing_baseline_agents)
        )
    return "\n".join(lines) + "\n"


def _failure_text(row: ReportRow) -> str:
    return ", ".join(f"{reason} ({count})" for reason, count in row.failure_reasons) or "none"


def render_markdown(report: Report) -> str:
    lines = [
        "# AgentAblate report",
        "",
        f"- Config hash: {', '.join(report.config_hashes) or 'none'}",
        "- Adapter implementation/version: "
        + (
            ", ".join(f"{kind} / {version}" for kind, version in report.adapter_implementations)
            or "none"
        ),
        f"- Repetitions: {report.repetitions}",
        "",
    ]
    if not report.rows:
        return "\n".join([*lines, "No trial results.\n"])
    if not report.has_baseline:
        lines.extend(["No baseline variant was found.", ""])
    lines.extend(
        [
            "| Agent | Variant | Task | Success | Success rate | "
            "Mean duration (s) | Failure reason |",
            "|---|---|---|---:|---:|---:|---|",
        ]
    )
    for row in report.rows:
        lines.append(
            f"| {_markdown_cell(row.agent_id)} | {_markdown_cell(row.variant_id)} | "
            f"{_markdown_cell(', '.join(row.task_ids))} | "
            f"{row.success_count}/{row.trial_count} | {row.success_rate:.1%} | "
            f"{row.mean_duration:.3f} | {_markdown_cell(_failure_text(row))} |"
        )
    return "\n".join(lines) + "\n"


def render_html(report: Report) -> str:
    environment = Environment(
        loader=FileSystemLoader(Path(__file__).with_name("templates")),
        autoescape=select_autoescape(("html",)),
        keep_trailing_newline=True,
    )
    return environment.get_template("report.html.j2").render(
        report=report, failure_text=_failure_text
    )
