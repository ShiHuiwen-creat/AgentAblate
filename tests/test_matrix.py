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
    task = TaskSpec(
        id="fix-bug",
        repo=tmp_path / "repo",
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
    copied_task = bundle.tasks[0].model_copy(
        update={"repo": tmp_path / "copied" / "repo"}
    )
    copied_bundle = bundle.model_copy(
        update={"config": copied_config, "tasks": (copied_task,)}
    )

    assert [trial.id for trial in expand_matrix(bundle)] == [
        trial.id for trial in expand_matrix(copied_bundle)
    ]
