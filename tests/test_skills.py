import os
from pathlib import Path

import pytest

from agentablate.skills import inspect_skill, inspect_skills


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
