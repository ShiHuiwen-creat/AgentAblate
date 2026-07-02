import sqlite3
from contextlib import closing
from pathlib import Path

from agentablate.reporting import (
    load_comparison,
    load_report,
    render_comparison,
    render_html,
    render_markdown,
)


def _database(tmp_path: Path) -> Path:
    path = tmp_path / "results.sqlite3"
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.executescript(
            """
            CREATE TABLE experiments (
              name TEXT PRIMARY KEY, config_hash TEXT NOT NULL,
              source TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE trials (
              id TEXT PRIMARY KEY, experiment TEXT NOT NULL, agent_id TEXT NOT NULL,
              variant_id TEXT NOT NULL, task_id TEXT NOT NULL, repetition INTEGER NOT NULL,
              status TEXT NOT NULL, success INTEGER, duration_seconds REAL,
              error TEXT, config_hash TEXT NOT NULL,
              adapter_type TEXT NOT NULL, implementation_version TEXT NOT NULL
            );
            INSERT INTO experiments VALUES ('demo', 'hash-1', 'agentablate.yaml', 'now');
            INSERT INTO trials VALUES
              ('1','demo','fake','baseline','task-b',0,'completed',1,2.0,NULL,
               'hash-1','fake','0.1.0.dev0'),
              ('2','demo','fake','with-skill','task-a',0,'failed',0,4.0,'timeout',
               'hash-1','fake','0.1.0.dev0'),
              ('3','demo','fake','with-skill','task-a',1,'completed',1,6.0,NULL,
               'hash-1','fake','0.1.0.dev0');
            """
        )
    return path


def test_load_report_aggregates_deterministically(tmp_path: Path) -> None:
    report = load_report(_database(tmp_path))

    assert [row.variant_id for row in report.rows] == ["baseline", "with-skill"]
    assert report.rows[1].success_count == 1
    assert report.rows[1].success_rate == 0.5
    assert report.rows[1].mean_duration == 5.0
    assert report.rows[1].failure_reasons == (("timeout", 1),)
    assert report.config_hashes == ("hash-1",)
    assert report.repetitions == 2
    assert report.adapter_implementations == (("fake", "0.1.0.dev0"),)


def test_comparison_reports_exact_baseline_deltas(tmp_path: Path) -> None:
    comparison = load_comparison(_database(tmp_path))

    assert len(comparison.rows) == 1
    row = comparison.rows[0]
    assert (row.agent_id, row.variant_id) == ("fake", "with-skill")
    assert (row.baseline_success_count, row.baseline_trial_count) == (1, 1)
    assert (row.variant_success_count, row.variant_trial_count) == (1, 2)
    assert row.success_count_delta == 0
    assert row.success_rate_delta == -0.5
    rendered = render_comparison(comparison)
    assert "1/1 (100.0%)" in rendered
    assert "1/2 (50.0%)" in rendered
    assert "-50.0 pp" in rendered


def test_renderers_include_required_metadata_without_absolute_paths(tmp_path: Path) -> None:
    report = load_report(_database(tmp_path))

    markdown = render_markdown(report)
    html = render_html(report)

    required = (
        "hash-1", "fake", "0.1.0.dev0", "with-skill", "task-a",
        "50.0%", "5.000", "timeout",
    )
    for value in required:
        assert value in markdown
        assert value in html
    assert str(tmp_path) not in markdown + html
    assert "<style>" in html and "http" not in html


def test_empty_database_and_missing_baseline_are_explicit(tmp_path: Path) -> None:
    path = _database(tmp_path)
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("DELETE FROM trials")
    empty = load_report(path)
    assert empty.rows == ()
    assert "No trial results" in render_markdown(empty)

    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            "INSERT INTO trials VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "x", "demo", "fake", "other", "task", 0, "failed", 0,
                1.0, "boom", "hash-1", "fake", "0.1.0.dev0",
            ),
        )
    no_baseline = load_report(path)
    assert no_baseline.has_baseline is False
    assert "No baseline variant" in render_markdown(no_baseline)
