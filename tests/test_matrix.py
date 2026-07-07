import subprocess
from pathlib import Path

import pytest

from agentablate.matrix import expand_matrix, fingerprint_path
from agentablate.models import (
    AgentConfig,
    ExperimentBundle,
    ExperimentConfig,
    ExperimentMeta,
    TaskSpec,
    VariantConfig,
)


@pytest.fixture
def bundle(tmp_path: Path) -> ExperimentBundle:
    skill = tmp_path / "skill"
    skill.mkdir()
    (skill / "SKILL.md").write_text("version one")
    config = ExperimentConfig(
        version=1,
        experiment=ExperimentMeta(name="demo", repetitions=2),
        agents=(AgentConfig(id="fake", adapter="fake"),),
        variants=(
            VariantConfig(id="baseline"),
            VariantConfig(id="with-skill", skills=(skill,)),
        ),
        tasks=(),
    )
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(("git", "init"), cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ("git", "config", "user.name", "AgentAblate Tests"),
        cwd=repo,
        check=True,
    )
    subprocess.run(
        ("git", "config", "user.email", "tests@example.com"),
        cwd=repo,
        check=True,
    )
    (repo / "tracked.txt").write_text("one\n")
    subprocess.run(("git", "add", "tracked.txt"), cwd=repo, check=True)
    subprocess.run(("git", "commit", "-m", "initial"), cwd=repo, check=True)
    task = TaskSpec(
        id="fix-bug",
        repo=repo,
        prompt="Fix the bug.",
        test_command=("python", "-m", "pytest", "-q"),
    )
    return ExperimentBundle(
        source=tmp_path / "agentablate.yaml",
        config_hash="config-hash",
        config=config,
        tasks=(task,),
    )


def test_expand_matrix_is_deterministic(bundle: ExperimentBundle) -> None:
    first = expand_matrix(bundle)
    second = expand_matrix(bundle)

    assert len(first) == 4
    assert [trial.id for trial in first] == [trial.id for trial in second]
    assert len(set(trial.id for trial in first)) == 4
    assert all(len(trial.id) == 16 for trial in first)
    assert first[0].id != first[2].id


def test_skill_content_changes_only_its_variant_ids(
    bundle: ExperimentBundle,
) -> None:
    before = expand_matrix(bundle)
    skill_file = bundle.config.variants[1].skills[0] / "SKILL.md"
    skill_file.write_text("version two")

    after = expand_matrix(bundle)

    baseline_before = [trial.id for trial in before if trial.variant.id == "baseline"]
    baseline_after = [trial.id for trial in after if trial.variant.id == "baseline"]
    skill_before = [trial.id for trial in before if trial.variant.id == "with-skill"]
    skill_after = [trial.id for trial in after if trial.variant.id == "with-skill"]
    assert baseline_before == baseline_after
    assert skill_before != skill_after


def test_directory_fingerprint_preserves_file_boundaries(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    (first / "a").write_text("bc")
    (second / "ab").write_text("c")

    assert fingerprint_path(first) != fingerprint_path(second)


def test_trial_ids_are_stable_when_experiment_root_moves(
    bundle: ExperimentBundle,
    tmp_path: Path,
) -> None:
    copied_skill = tmp_path / "copied" / "skill"
    copied_skill.mkdir(parents=True)
    (copied_skill / "SKILL.md").write_text("version one")
    copied_variants = (
        VariantConfig(id="baseline"),
        VariantConfig(id="with-skill", skills=(copied_skill,)),
    )
    copied_config = bundle.config.model_copy(update={"variants": copied_variants})
    copied_repo = tmp_path / "copied" / "repo"
    subprocess.run(
        ("git", "clone", "--quiet", str(bundle.tasks[0].repo), str(copied_repo)),
        check=True,
    )
    copied_task = bundle.tasks[0].model_copy(update={"repo": copied_repo})
    copied_bundle = bundle.model_copy(
        update={"config": copied_config, "tasks": (copied_task,)}
    )

    assert [trial.id for trial in expand_matrix(bundle)] == [
        trial.id for trial in expand_matrix(copied_bundle)
    ]


def test_expand_matrix_binds_revision_to_commit_oid(bundle: ExperimentBundle) -> None:
    repo = bundle.tasks[0].repo
    expected = subprocess.run(
        ("git", "rev-parse", "HEAD"),
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    trial = expand_matrix(bundle)[0]

    assert trial.task.revision == expected
    assert len(trial.task.revision) == 40


def test_evaluator_environment_changes_trial_identity(
    bundle: ExperimentBundle, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "agentablate.matrix.evaluator_environment_identity",
        lambda: {"python_version": "3.12.1", "schema": 1},
    )
    first = expand_matrix(bundle)
    monkeypatch.setattr(
        "agentablate.matrix.evaluator_environment_identity",
        lambda: {"python_version": "3.13.0", "schema": 1},
    )
    second = expand_matrix(bundle)

    assert first[0].evaluator_hash
    assert first[0].evaluator_hash != second[0].evaluator_hash
    assert first[0].id != second[0].id
