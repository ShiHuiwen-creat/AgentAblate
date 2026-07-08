import os
import subprocess
from pathlib import Path

import pytest

from agentablate.adapters.codex_exec import CODEX_EXEC_POLICY
from agentablate.matrix import _trial_id, expand_matrix, fingerprint_path
from agentablate.models import (
    AdapterRuntimeIdentity,
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
    (skill / "SKILL.md").write_text("---\nname: reviewer\ndescription: Test\n---\nversion one\n")
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
    skill_file.write_text("---\nname: reviewer\ndescription: Test\n---\nversion two\n")

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


@pytest.mark.skipif(os.name != "posix", reason="POSIX executable bits required")
def test_directory_fingerprint_binds_executable_mode(tmp_path: Path) -> None:
    directory = tmp_path / "extension"
    directory.mkdir()
    script = directory / "run.sh"
    script.write_text("exit 0\n")
    script.chmod(0o755)
    executable = fingerprint_path(directory)
    script.chmod(0o644)

    assert fingerprint_path(directory) != executable


def test_trial_ids_are_stable_when_experiment_root_moves(
    bundle: ExperimentBundle,
    tmp_path: Path,
) -> None:
    copied_skill = tmp_path / "copied" / "skill"
    copied_skill.mkdir(parents=True)
    (copied_skill / "SKILL.md").write_text(
        "---\nname: reviewer\ndescription: Test\n---\nversion one\n"
    )
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
    copied_bundle = bundle.model_copy(update={"config": copied_config, "tasks": (copied_task,)})

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


def test_evaluator_identity_changes_trial_identity(
    bundle: ExperimentBundle, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "agentablate.matrix.evaluator_identity",
        lambda command: {"executable_sha256": "first", "schema": 2},
    )
    first = expand_matrix(bundle)
    monkeypatch.setattr(
        "agentablate.matrix.evaluator_identity",
        lambda command: {"executable_sha256": "second", "schema": 2},
    )
    second = expand_matrix(bundle)

    assert first[0].evaluator_hash
    assert first[0].evaluator_hash != second[0].evaluator_hash
    assert first[0].id != second[0].id


def test_each_task_uses_its_actual_evaluator_identity(
    bundle: ExperimentBundle, monkeypatch: pytest.MonkeyPatch
) -> None:
    other = bundle.tasks[0].model_copy(
        update={"id": "other", "test_command": ("different-evaluator",)}
    )
    bundle = bundle.model_copy(update={"tasks": (*bundle.tasks, other)})
    seen: list[tuple[str, ...]] = []

    def identity(command: tuple[str, ...]) -> dict[str, object]:
        seen.append(command)
        return {"command": command[0]}

    monkeypatch.setattr("agentablate.matrix.evaluator_identity", identity)

    trials = expand_matrix(bundle)

    hashes = {trial.task.id: trial.evaluator_hash for trial in trials}
    assert hashes["fix-bug"] != hashes["other"]
    assert set(seen) == {bundle.tasks[0].test_command, other.test_command}


def test_expand_matrix_refreshes_default_dependency_identity(
    bundle: ExperimentBundle, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Distribution:
        metadata = {"Name": "changing-package"}

        def __init__(self, version: str) -> None:
            self.version = version

    task = bundle.tasks[0].model_copy(update={"test_command": ("missing-checker",)})
    bundle = bundle.model_copy(update={"tasks": (task,)})

    monkeypatch.setattr(
        "agentablate.identity.importlib.metadata.distributions",
        lambda: [Distribution("1.0")],
    )
    before = expand_matrix(bundle)
    monkeypatch.setattr(
        "agentablate.identity.importlib.metadata.distributions",
        lambda: [Distribution("2.0")],
    )
    after = expand_matrix(bundle)

    assert before[0].evaluator_hash != after[0].evaluator_hash
    assert before[0].id != after[0].id


def _codex_bundle(bundle: ExperimentBundle) -> ExperimentBundle:
    config = bundle.config.model_copy(
        update={"agents": (AgentConfig(id="codex", adapter="codex-exec"),)}
    )
    return bundle.model_copy(update={"config": config})


def test_codex_runtime_changes_trial_id(
    bundle: ExperimentBundle, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "agentablate.matrix.discover_codex_runtime",
        lambda: AdapterRuntimeIdentity(
            schema_version=1,
            executable="/tools/codex",
            executable_basename="codex",
            executable_sha256="a" * 64,
            version="codex-cli 1.0.0",
            policy=CODEX_EXEC_POLICY,
            ambient_skills_sha256="b" * 64,
        ),
    )
    first = expand_matrix(_codex_bundle(bundle))[0]
    monkeypatch.setattr(
        "agentablate.matrix.discover_codex_runtime",
        lambda: first.adapter_runtime.model_copy(update={"executable_sha256": "c" * 64}),
    )
    second = expand_matrix(_codex_bundle(bundle))[0]

    assert first.id != second.id


def test_matrix_freezes_structured_skill_inputs_once_per_variant(
    bundle: ExperimentBundle, monkeypatch: pytest.MonkeyPatch
) -> None:
    import agentablate.matrix as matrix

    real_inspect = matrix.inspect_skills
    calls: list[tuple[Path, ...]] = []

    def inspect(paths: tuple[Path, ...]):
        calls.append(paths)
        return real_inspect(paths)

    skill = bundle.config.variants[1].skills[0]
    skill.joinpath("SKILL.md").write_text(
        "---\nname: reviewer\ndescription: Test\n---\nInstructions.\n"
    )
    monkeypatch.setattr(matrix, "inspect_skills", inspect)

    trials = expand_matrix(bundle)

    assert calls == [(), (skill,)]
    with_skill = next(trial for trial in trials if trial.variant.id == "with-skill")
    assert tuple(identity.name for identity in with_skill.skill_inputs) == ("reviewer",)
    assert all(trial.adapter_runtime is None for trial in trials)


@pytest.mark.parametrize("variant_index", (0, 1))
def test_non_codex_trial_id_matches_phase_one_payload(
    bundle: ExperimentBundle, variant_index: int
) -> None:
    trial = next(
        trial
        for trial in expand_matrix(bundle)
        if trial.variant.id == bundle.config.variants[variant_index].id
        and trial.repetition == 0
    )
    skill_hashes = tuple(
        fingerprint_path(path) for path in trial.variant.skills
    )
    mcp_hashes = tuple(fingerprint_path(path) for path in trial.variant.mcp)
    phase_one_payload = {
        "config_hash": bundle.config_hash,
        "experiment": bundle.config.experiment.name,
        "agent": trial.agent.model_dump(mode="json"),
        "variant": {
            "id": trial.variant.id,
            "skill_hashes": skill_hashes,
            "mcp_hashes": mcp_hashes,
        },
        "task": {
            "id": trial.task.id,
            "revision": trial.task.revision,
            "prompt": trial.task.prompt,
            "test_command": trial.task.test_command,
        },
        "repetition": trial.repetition,
        "evaluator_hash": trial.evaluator_hash,
    }

    assert trial.id == _trial_id(phase_one_payload)
