import hashlib
import json
import os
import re
import stat
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import yaml

from agentablate.models import SkillInputIdentity

_SKILL_NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


@dataclass(frozen=True, slots=True)
class SkillTree:
    identity: SkillInputIdentity
    source: Path


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
