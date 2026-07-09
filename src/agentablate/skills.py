import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
from collections.abc import Iterable
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import yaml

from agentablate.models import SkillInputIdentity

_SKILL_NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


@dataclass(frozen=True, slots=True)
class SkillTree:
    identity: SkillInputIdentity
    source: Path


@dataclass(slots=True)
class _InstallState:
    workspace: Path
    temporary: tempfile.TemporaryDirectory[str]
    backups: list[tuple[Path, Path]]
    installed_targets: list[Path]
    created_dirs: list[Path]


def _require_real_directory(metadata: os.stat_result, path: Path) -> None:
    if stat.S_ISLNK(metadata.st_mode):
        raise ValueError(f"skill root must not be a symlink: {path}")
    if not stat.S_ISDIR(metadata.st_mode):
        raise ValueError(f"skill root must be a directory: {path}")


def _regular_file_records(path: Path) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []

    def visit(directory: Path) -> None:
        with os.scandir(directory) as entries:
            for entry in sorted(entries, key=lambda item: item.name):
                entry_path = Path(entry.path)
                metadata = entry.stat(follow_symlinks=False)
                if stat.S_ISLNK(metadata.st_mode):
                    raise ValueError(f"skill tree must not contain a symlink: {entry_path}")
                if stat.S_ISDIR(metadata.st_mode):
                    visit(entry_path)
                elif stat.S_ISREG(metadata.st_mode):
                    records.append(
                        {
                            "path": entry_path.relative_to(path).as_posix(),
                            "sha256": hashlib.sha256(entry_path.read_bytes()).hexdigest(),
                            "mode": metadata.st_mode & 0o111,
                        }
                    )
                else:
                    raise ValueError(
                        "skill tree entries must be a regular file or directory: "
                        f"{entry_path}"
                    )

    visit(path)
    return sorted(records, key=lambda record: str(record["path"]))


def _records_hash(records: list[dict[str, object]]) -> str:
    encoded = json.dumps(records, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def fingerprint_tree(path: Path) -> str:
    _require_real_directory(os.lstat(path), path)
    source = path.resolve(strict=True)
    return _records_hash(_regular_file_records(source))


def _frontmatter_name(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise ValueError(f"invalid SKILL.md: {path}") from error
    lines = text.splitlines()
    if not lines or lines[0] != "---":
        raise ValueError(f"SKILL.md must start with YAML frontmatter: {path}")
    try:
        end = lines.index("---", 1)
    except ValueError as error:
        raise ValueError(f"SKILL.md frontmatter is not terminated: {path}") from error
    try:
        frontmatter = yaml.safe_load("\n".join(lines[1:end]))
    except yaml.YAMLError as error:
        raise ValueError(f"invalid SKILL.md frontmatter: {path}") from error
    name = frontmatter.get("name") if isinstance(frontmatter, dict) else None
    if not isinstance(name, str) or _SKILL_NAME.fullmatch(name) is None:
        raise ValueError(f"invalid skill name in SKILL.md: {name!r}")
    return name


def inspect_skill(path: Path) -> SkillTree:
    _require_real_directory(os.lstat(path), path)
    source = path.resolve(strict=True)
    records = _regular_file_records(source)
    name = _frontmatter_name(source / "SKILL.md")
    fingerprint = _records_hash(records)
    identity = SkillInputIdentity(
        name=name,
        fingerprint=fingerprint,
        install_name=f"{name}-{fingerprint[:12]}",
    )
    return SkillTree(identity, source)


def inspect_skills(paths: Iterable[Path]) -> tuple[SkillTree, ...]:
    trees = tuple(inspect_skill(path) for path in paths)
    names = [tree.identity.name for tree in trees]
    if len(names) != len(set(names)):
        raise ValueError("duplicate skill name")
    return trees


def _validate_frozen_inputs(
    paths: Iterable[Path], frozen_identities: Iterable[SkillInputIdentity]
) -> tuple[SkillTree, ...]:
    trees = tuple(inspect_skill(path) for path in paths)
    frozen = tuple(frozen_identities)
    if len(trees) != len(frozen):
        raise ValueError("skill paths and frozen identities must have the same length")
    for tree, identity in zip(trees, frozen, strict=True):
        if tree.identity.name != identity.name:
            raise ValueError(f"skill name mismatch for {tree.source}")
        if tree.identity.fingerprint != identity.fingerprint:
            raise ValueError(f"skill fingerprint mismatch for {tree.source}")
        if tree.identity.install_name != identity.install_name:
            raise ValueError(f"skill install-name mismatch for {tree.source}")
    return trees


def _ensure_directory_component(path: Path, root: Path, label: str) -> None:
    try:
        metadata = os.lstat(path)
    except FileNotFoundError:
        return
    if stat.S_ISLNK(metadata.st_mode):
        raise ValueError(f"{label} must not be a symlink: {path}")
    if not stat.S_ISDIR(metadata.st_mode):
        raise ValueError(f"{label} must be a directory: {path}")
    resolved = path.resolve(strict=True)
    if not resolved.is_relative_to(root):
        raise ValueError(f"{label} escapes workspace: {path}")


def _validate_existing_tree(path: Path, root: Path) -> None:
    try:
        metadata = os.lstat(path)
    except FileNotFoundError:
        return
    if stat.S_ISLNK(metadata.st_mode):
        raise ValueError(f"installed skill target must not be a symlink: {path}")
    if not stat.S_ISDIR(metadata.st_mode):
        raise ValueError(f"installed skill target must be a directory: {path}")
    if not path.resolve(strict=True).is_relative_to(root):
        raise ValueError(f"installed skill target escapes workspace: {path}")
    with os.scandir(path) as entries:
        for entry in entries:
            child = Path(entry.path)
            child_metadata = entry.stat(follow_symlinks=False)
            if stat.S_ISLNK(child_metadata.st_mode):
                raise ValueError(f"installed skill target must not contain a symlink: {child}")
            if stat.S_ISDIR(child_metadata.st_mode):
                _validate_existing_tree(child, root)
            elif not stat.S_ISREG(child_metadata.st_mode):
                raise ValueError(
                    "installed skill target entries must be a regular file or directory: "
                    f"{child}"
                )


def _prepare_install_root(workspace: Path) -> tuple[Path, list[Path]]:
    root = workspace.resolve(strict=True)
    agents = workspace / ".agents"
    skills = agents / "skills"
    _ensure_directory_component(agents, root, ".agents")
    _ensure_directory_component(skills, root, ".agents/skills")
    created: list[Path] = []
    for directory in (agents, skills):
        if not directory.exists():
            directory.mkdir()
            created.append(directory)
    return root, created


def _remove_tree(path: Path) -> None:
    try:
        metadata = os.lstat(path)
    except FileNotFoundError:
        return
    if stat.S_ISDIR(metadata.st_mode) and not stat.S_ISLNK(metadata.st_mode):
        shutil.rmtree(path)
    else:
        path.unlink()


def _copy_skill_tree(source: Path, destination: Path) -> None:
    source_metadata = os.lstat(source)
    if not stat.S_ISDIR(source_metadata.st_mode) or stat.S_ISLNK(source_metadata.st_mode):
        raise ValueError(f"skill source must be a real directory: {source}")
    destination.mkdir()
    shutil.copystat(source, destination, follow_symlinks=False)
    with os.scandir(source) as entries:
        for entry in sorted(entries, key=lambda item: item.name):
            source_child = Path(entry.path)
            destination_child = destination / entry.name
            metadata = entry.stat(follow_symlinks=False)
            if stat.S_ISLNK(metadata.st_mode):
                raise ValueError(f"skill tree must not contain a symlink: {source_child}")
            if stat.S_ISDIR(metadata.st_mode):
                _copy_skill_tree(source_child, destination_child)
            elif stat.S_ISREG(metadata.st_mode):
                shutil.copy2(source_child, destination_child, follow_symlinks=False)
            else:
                raise ValueError(
                    f"skill tree entries must be a regular file or directory: {source_child}"
                )


def _install_and_verify(trees: tuple[SkillTree, ...], workspace: Path) -> _InstallState:
    temporary = tempfile.TemporaryDirectory(prefix="agentablate-skills-")
    state = _InstallState(
        workspace=workspace,
        temporary=temporary,
        backups=[],
        installed_targets=[],
        created_dirs=[],
    )
    try:
        root, created = _prepare_install_root(workspace)
        state.created_dirs.extend(created)
        install_root = workspace / ".agents" / "skills"
        for index, tree in enumerate(trees):
            target = install_root / tree.identity.install_name
            _validate_existing_tree(target, root)
            backup = Path(temporary.name) / f"backup-{index}"
            if target.exists():
                shutil.copytree(target, backup, copy_function=shutil.copy2, symlinks=False)
                state.backups.append((target, backup))
                _remove_tree(target)
            _copy_skill_tree(tree.source, target)
            state.installed_targets.append(target)
            installed = inspect_skill(target)
            if installed.identity != tree.identity:
                raise ValueError(f"installed skill fingerprint mismatch for {tree.identity.name}")
        return state
    except BaseException as error:
        try:
            _restore(state)
        except BaseException as rollback:
            temporary.cleanup()
            raise BaseExceptionGroup(
                "skill installation and rollback failed", [error, rollback]
            ) from None
        temporary.cleanup()
        raise


def _restore(state: _InstallState) -> None:
    for target in reversed(state.installed_targets):
        _remove_tree(target)
    for target, backup in reversed(state.backups):
        if target.exists():
            _remove_tree(target)
        shutil.copytree(backup, target, copy_function=shutil.copy2, symlinks=False)
    for directory in reversed(state.created_dirs):
        try:
            directory.rmdir()
        except FileNotFoundError:
            pass
        except OSError:
            pass
    state.temporary.cleanup()


@contextmanager
def installed_skills(
    paths: Iterable[Path],
    frozen_identities: Iterable[SkillInputIdentity],
    workspace: Path,
):
    installed = _validate_frozen_inputs(paths, frozen_identities)
    state = _install_and_verify(installed, workspace)
    primary: BaseException | None = None
    try:
        yield installed
    except BaseException as error:
        primary = error
    try:
        _restore(state)
    except BaseException as cleanup:
        if primary is not None:
            raise BaseExceptionGroup(
                "skill execution and restoration failed", [primary, cleanup]
            ) from None
        raise
    if primary is not None:
        raise primary.with_traceback(primary.__traceback__)
