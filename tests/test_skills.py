import os
import shutil
from pathlib import Path

import pytest

from agentablate.skills import inspect_skill, inspect_skills, installed_skills


def write_skill(path: Path, name: str) -> Path:
    path.mkdir()
    (path / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: Test skill\n---\n\nInstructions.\n",
        encoding="utf-8",
    )
    return path


def test_skill_identity_has_safe_content_addressed_install_name(
    tmp_path: Path,
) -> None:
    tree = inspect_skill(write_skill(tmp_path / "skill", "reviewer"))

    assert tree.identity.name == "reviewer"
    assert tree.identity.install_name == f"reviewer-{tree.identity.fingerprint[:12]}"
    assert len(tree.identity.fingerprint) == 64
    assert tree.source == (tmp_path / "skill").resolve()


@pytest.mark.skipif(os.name != "posix", reason="POSIX executable bits required")
def test_skill_fingerprint_binds_content_and_mode(tmp_path: Path) -> None:
    skill = write_skill(tmp_path / "skill", "reviewer")
    first = inspect_skill(skill)
    script = skill / "scripts" / "run.sh"
    script.parent.mkdir()
    script.write_text("exit 0\n", encoding="utf-8")
    script.chmod(0o755)
    executable = inspect_skill(skill)
    script.chmod(0o644)
    non_executable = inspect_skill(skill)

    assert first.identity.fingerprint != executable.identity.fingerprint
    assert executable.identity.fingerprint != non_executable.identity.fingerprint


def test_inspect_skill_rejects_missing_skill_document(tmp_path: Path) -> None:
    skill = tmp_path / "skill"
    skill.mkdir()

    with pytest.raises(ValueError, match="SKILL.md"):
        inspect_skill(skill)


def test_inspect_skills_rejects_duplicate_names(tmp_path: Path) -> None:
    first = write_skill(tmp_path / "first", "reviewer")
    second = write_skill(tmp_path / "second", "reviewer")

    with pytest.raises(ValueError, match="duplicate skill name"):
        inspect_skills((first, second))


def test_inspect_skill_rejects_symlink_root(tmp_path: Path) -> None:
    target = write_skill(tmp_path / "target", "reviewer")
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)

    with pytest.raises(ValueError, match="symlink"):
        inspect_skill(link)


def test_inspect_skill_rejects_nested_symlink(tmp_path: Path) -> None:
    skill = write_skill(tmp_path / "skill", "reviewer")
    (skill / "linked").symlink_to(skill / "SKILL.md")

    with pytest.raises(ValueError, match="symlink"):
        inspect_skill(skill)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO unsupported")
def test_inspect_skill_rejects_fifo(tmp_path: Path) -> None:
    skill = write_skill(tmp_path / "skill", "reviewer")
    os.mkfifo(skill / "pipe")

    with pytest.raises(ValueError, match="regular file or directory"):
        inspect_skill(skill)


@pytest.mark.parametrize(
    "name",
    ("Reviewer", "reviewer_skill", "reviewer--skill", "-reviewer", "reviewer-"),
)
def test_inspect_skill_rejects_unsafe_name(tmp_path: Path, name: str) -> None:
    skill = write_skill(tmp_path / "skill", name)

    with pytest.raises(ValueError, match="skill name"):
        inspect_skill(skill)


def test_installed_skills_copies_to_agents_skills_and_cleans_up(tmp_path: Path) -> None:
    skill = write_skill(tmp_path / "skill", "reviewer")
    script = skill / "scripts" / "run.sh"
    script.parent.mkdir()
    script.write_text("exit 0\n", encoding="utf-8")
    script.chmod(0o755)
    tree = inspect_skill(skill)
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    with installed_skills((skill,), (tree.identity,), workspace) as installed:
        target = workspace / ".agents" / "skills" / installed[0].identity.install_name
        assert (target / "SKILL.md").read_bytes() == (skill / "SKILL.md").read_bytes()
        assert (target / "scripts" / "run.sh").stat().st_mode & 0o111

    assert not target.exists()
    assert not (workspace / ".agents").exists()


def test_installed_skills_restores_preexisting_target_bytes_and_mode(tmp_path: Path) -> None:
    skill = write_skill(tmp_path / "skill", "reviewer")
    tree = inspect_skill(skill)
    workspace = tmp_path / "workspace"
    target = workspace / ".agents" / "skills" / tree.identity.install_name
    target.mkdir(parents=True)
    existing = target / "SKILL.md"
    existing.write_text("pre-existing\n", encoding="utf-8")
    existing.chmod(0o600)

    with installed_skills((skill,), (tree.identity,), workspace):
        assert existing.read_text(encoding="utf-8") != "pre-existing\n"

    assert existing.read_text(encoding="utf-8") == "pre-existing\n"
    assert existing.stat().st_mode & 0o777 == 0o600


@pytest.mark.asyncio
async def test_installed_skills_cleans_up_after_cancellation(tmp_path: Path) -> None:
    skill = write_skill(tmp_path / "skill", "reviewer")
    tree = inspect_skill(skill)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / ".agents" / "skills" / tree.identity.install_name

    with pytest.raises(KeyboardInterrupt), installed_skills((skill,), (tree.identity,), workspace):
        assert target.exists()
        raise KeyboardInterrupt

    assert not target.exists()


def test_installed_skills_rejects_preexisting_symlink_and_never_reads_through_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    skill = write_skill(tmp_path / "skill", "reviewer")
    tree = inspect_skill(skill)
    workspace = tmp_path / "workspace"
    sensitive = tmp_path / "outside-secret"
    sensitive.write_text("secret", encoding="utf-8")
    target = workspace / ".agents" / "skills" / tree.identity.install_name
    target.parent.mkdir(parents=True)
    target.symlink_to(sensitive)
    original_read_bytes = Path.read_bytes
    reads: list[Path] = []

    def read_bytes(path: Path) -> bytes:
        reads.append(path)
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", read_bytes)

    with pytest.raises(ValueError, match="symlink"), installed_skills(
        (skill,), (tree.identity,), workspace
    ):
        raise AssertionError("unreachable")

    assert sensitive.read_text(encoding="utf-8") == "secret"
    assert sensitive not in reads


def test_installed_skills_rejects_special_component_without_escape(tmp_path: Path) -> None:
    skill = write_skill(tmp_path / "skill", "reviewer")
    tree = inspect_skill(skill)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".agents").write_text("not a directory", encoding="utf-8")

    with pytest.raises(ValueError, match="directory"), installed_skills(
        (skill,), (tree.identity,), workspace
    ):
        raise AssertionError("unreachable")


def test_installed_skills_source_mutation_before_copy_cleans_up(tmp_path: Path) -> None:
    skill = write_skill(tmp_path / "skill", "reviewer")
    frozen = inspect_skill(skill).identity
    (skill / "SKILL.md").write_text(
        "---\nname: reviewer\ndescription: changed\n---\n\nChanged.\n",
        encoding="utf-8",
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    with pytest.raises(ValueError, match="fingerprint"), installed_skills(
        (skill,), (frozen,), workspace
    ):
        raise AssertionError("unreachable")

    assert not (workspace / ".agents").exists()


def test_installed_skills_copy_failure_restores_preexisting_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    skill = write_skill(tmp_path / "skill", "reviewer")
    extra = skill / "scripts" / "run.sh"
    extra.parent.mkdir()
    extra.write_text("exit 0\n", encoding="utf-8")
    tree = inspect_skill(skill)
    workspace = tmp_path / "workspace"
    target = workspace / ".agents" / "skills" / tree.identity.install_name
    target.mkdir(parents=True)
    existing = target / "SKILL.md"
    existing.write_text("pre-existing\n", encoding="utf-8")
    existing.chmod(0o600)
    original_copy2 = shutil.copy2

    def copy2(src: Path | str, dst: Path | str, *, follow_symlinks: bool = True):
        if Path(src).name == "run.sh":
            raise OSError("copy interrupted")
        return original_copy2(src, dst, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(shutil, "copy2", copy2)

    with pytest.raises(OSError, match="copy interrupted"), installed_skills(
        (skill,), (tree.identity,), workspace
    ):
        raise AssertionError("unreachable")

    assert existing.read_text(encoding="utf-8") == "pre-existing\n"
    assert existing.stat().st_mode & 0o777 == 0o600
    assert not (target / "scripts").exists()


def test_installed_skills_copy_failure_removes_partial_new_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    skill = write_skill(tmp_path / "skill", "reviewer")
    extra = skill / "scripts" / "run.sh"
    extra.parent.mkdir()
    extra.write_text("exit 0\n", encoding="utf-8")
    tree = inspect_skill(skill)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / ".agents" / "skills" / tree.identity.install_name
    original_copy2 = shutil.copy2

    def copy2(src: Path | str, dst: Path | str, *, follow_symlinks: bool = True):
        if Path(src).name == "run.sh":
            raise OSError("copy interrupted")
        return original_copy2(src, dst, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(shutil, "copy2", copy2)

    with pytest.raises(OSError, match="copy interrupted"), installed_skills(
        (skill,), (tree.identity,), workspace
    ):
        raise AssertionError("unreachable")

    assert not target.exists()
    assert not (workspace / ".agents").exists()
