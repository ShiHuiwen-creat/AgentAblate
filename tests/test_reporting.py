import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

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
              adapter_type TEXT NOT NULL, implementation_version TEXT NOT NULL,
              exit_code INTEGER, started_at TEXT NOT NULL DEFAULT 'now', completed_at TEXT
            );
            INSERT INTO experiments VALUES ('demo', 'hash-1', 'agentablate.yaml', 'now');
            INSERT INTO trials(
              id,experiment,agent_id,variant_id,task_id,repetition,status,success,
              duration_seconds,error,config_hash,adapter_type,implementation_version
            ) VALUES
              ('1','demo','fake','baseline','task-a',0,'completed',1,2.0,NULL,
               'hash-1','fake','0.1.0.dev0'),
              ('2','demo','fake','with-skill','task-a',0,'completed',0,4.0,'timeout',
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
    assert (row.variant_success_count, row.variant_trial_count) == (0, 1)
    assert row.success_count_delta == -1
    assert row.success_rate_delta == -1.0
    rendered = render_comparison(comparison)
    assert "1/1 (100.0%)" in rendered
    assert "0/1 (0.0%)" in rendered
    assert "-100.0 pp" in rendered


def test_renderers_include_required_metadata_without_absolute_paths(tmp_path: Path) -> None:
    report = load_report(_database(tmp_path))

    markdown = render_markdown(report)
    html = render_html(report)

    required = (
        "hash-1",
        "fake",
        "0.1.0.dev0",
        "with-skill",
        "task-a",
        "50.0%",
        "5.000",
        "timeout",
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
            """INSERT INTO trials(
              id,experiment,agent_id,variant_id,task_id,repetition,status,success,
              duration_seconds,error,config_hash,adapter_type,implementation_version
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "x",
                "demo",
                "fake",
                "other",
                "task",
                0,
                "completed",
                0,
                1.0,
                "boom",
                "hash-1",
                "fake",
                "0.1.0.dev0",
            ),
        )
    no_baseline = load_report(path)
    assert no_baseline.has_baseline is False
    assert "No baseline variant" in render_markdown(no_baseline)


def test_report_uses_only_completed_trials_from_current_experiment_config(tmp_path: Path) -> None:
    path = _database(tmp_path)
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("UPDATE experiments SET config_hash='current'")
        connection.execute("UPDATE trials SET config_hash='old'")
        connection.execute(
            """INSERT INTO trials(
              id,experiment,agent_id,variant_id,task_id,repetition,status,success,
              duration_seconds,error,config_hash,adapter_type,implementation_version
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "current",
                "demo",
                "fake",
                "baseline",
                "task",
                0,
                "completed",
                1,
                3.0,
                None,
                "current",
                "fake",
                "0.1.0.dev0",
            ),
        )
        connection.execute(
            """INSERT INTO trials(
              id,experiment,agent_id,variant_id,task_id,repetition,status,success,
              duration_seconds,error,config_hash,adapter_type,implementation_version
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "running",
                "demo",
                "fake",
                "baseline",
                "task",
                1,
                "running",
                None,
                None,
                None,
                "current",
                "fake",
                "0.1.0.dev0",
            ),
        )

    report = load_report(path)

    assert len(report.rows) == 1
    assert report.rows[0].trial_count == 1
    assert report.config_hashes == ("current",)


def test_comparison_pairs_only_common_task_repetitions(tmp_path: Path) -> None:
    path = _database(tmp_path)
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("DELETE FROM trials")
        rows = (
            (
                "b0",
                "demo",
                "fake",
                "baseline",
                "same",
                0,
                "completed",
                1,
                1.0,
                None,
                "hash-1",
                "fake",
                "v",
            ),
            (
                "v0",
                "demo",
                "fake",
                "skill",
                "same",
                0,
                "completed",
                0,
                1.0,
                None,
                "hash-1",
                "fake",
                "v",
            ),
            (
                "b1",
                "demo",
                "fake",
                "baseline",
                "baseline-only",
                0,
                "completed",
                1,
                1.0,
                None,
                "hash-1",
                "fake",
                "v",
            ),
            (
                "v1",
                "demo",
                "fake",
                "skill",
                "variant-only",
                0,
                "completed",
                1,
                1.0,
                None,
                "hash-1",
                "fake",
                "v",
            ),
            (
                "run",
                "demo",
                "fake",
                "skill",
                "same",
                1,
                "running",
                None,
                None,
                None,
                "hash-1",
                "fake",
                "v",
            ),
        )
        connection.executemany(
            """INSERT INTO trials(
              id,experiment,agent_id,variant_id,task_id,repetition,status,success,
              duration_seconds,error,config_hash,adapter_type,implementation_version
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            rows,
        )

    row = load_comparison(path).rows[0]

    assert (row.baseline_trial_count, row.variant_trial_count) == (1, 1)
    assert row.success_count_delta == -1
    assert row.success_rate_delta == -1.0


def test_markdown_renderers_escape_dynamic_table_cells(tmp_path: Path) -> None:
    path = _database(tmp_path)
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("UPDATE trials SET agent_id=?, variant_id=?", ("a|b", "v\\x\nnext"))
    report_text = render_markdown(load_report(path))
    comparison_text = render_comparison(load_comparison(path))

    assert "a\\|b" in report_text + comparison_text
    assert "v\\\\x<br>next" in report_text + comparison_text


@pytest.mark.parametrize("loader", [load_report, load_comparison])
def test_reporting_rejects_missing_database_without_creating_it(
    tmp_path: Path,
    loader,
) -> None:
    path = tmp_path / "missing.sqlite3"

    with pytest.raises(FileNotFoundError):
        loader(path)

    assert not path.exists()


@pytest.mark.parametrize("loader", [load_report, load_comparison])
def test_reporting_migrates_v3_database_before_query(tmp_path: Path, loader) -> None:
    path = tmp_path / "v3.sqlite3"
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.executescript(
            """
            PRAGMA user_version = 3;
            CREATE TABLE experiments (
              name TEXT PRIMARY KEY, config_hash TEXT NOT NULL,
              source TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE trials (
              id TEXT PRIMARY KEY, experiment TEXT NOT NULL, agent_id TEXT NOT NULL,
              variant_id TEXT NOT NULL, task_id TEXT NOT NULL, repetition INTEGER NOT NULL,
              status TEXT NOT NULL, success INTEGER, duration_seconds REAL, exit_code INTEGER,
              error TEXT, started_at TEXT NOT NULL, completed_at TEXT,
              config_hash TEXT NOT NULL, extension_hashes TEXT NOT NULL,
              stdout TEXT NOT NULL DEFAULT '', stderr TEXT NOT NULL DEFAULT '',
              attempt_id TEXT, heartbeat_at TEXT
            );
            INSERT INTO experiments VALUES ('demo','hash','config.yaml','now');
            INSERT INTO trials VALUES
              ('x','demo','fake','baseline','task',0,'completed',1,1.0,0,NULL,
               'now','now','hash','[]','','',NULL,NULL);
            """
        )

    loader(path)

    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 4
