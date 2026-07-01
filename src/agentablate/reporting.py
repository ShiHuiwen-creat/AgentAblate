import sqlite3
from collections import Counter
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from agentablate import __version__


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
    version: str = __version__


def _failure_category(error: str | None) -> str:
    if not error:
        return "unspecified failure"
    return error.split(":", 1)[0].strip()


def load_report(database: Path) -> Report:
    """Read trial details and aggregate them without exposing stored source paths."""
    with closing(sqlite3.connect(database)) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """SELECT agent_id, variant_id, task_id, repetition, success,
                      duration_seconds, error, config_hash
               FROM trials
               ORDER BY agent_id, variant_id, task_id, repetition, id"""
        ).fetchall()

    groups: dict[tuple[str, str], list[sqlite3.Row]] = {}
    for row in rows:
        groups.setdefault((row["agent_id"], row["variant_id"]), []).append(row)

    aggregates: list[ReportRow] = []
    for (agent_id, variant_id), trials in sorted(groups.items()):
        successes = sum(row["success"] == 1 for row in trials)
        durations = [
            row["duration_seconds"]
            for row in trials
            if row["duration_seconds"] is not None
        ]
        failures = Counter(
            _failure_category(row["error"])
            for row in trials
            if row["success"] != 1
        )
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
    )


def _failure_text(row: ReportRow) -> str:
    return ", ".join(f"{reason} ({count})" for reason, count in row.failure_reasons) or "none"


def render_markdown(report: Report) -> str:
    lines = [
        "# AgentAblate report",
        "",
        f"- Config hash: {', '.join(report.config_hashes) or 'none'}",
        f"- Adapter/version: agent_id / agentablate {report.version}",
        f"- Repetitions: {report.repetitions}",
        "",
    ]
    if not report.rows:
        return "\n".join([*lines, "No trial results.\n"])
    if not report.has_baseline:
        lines.extend(["No baseline variant was found.", ""])
    lines.extend([
        "| Agent | Variant | Task | Success | Success rate | Mean duration (s) | Failure reason |",
        "|---|---|---|---:|---:|---:|---|",
    ])
    for row in report.rows:
        lines.append(
            f"| {row.agent_id} | {row.variant_id} | {', '.join(row.task_ids)} | "
            f"{row.success_count}/{row.trial_count} | {row.success_rate:.1%} | "
            f"{row.mean_duration:.3f} | {_failure_text(row)} |"
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
