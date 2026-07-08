import hashlib
import importlib.metadata
import json
import platform
import re
import shutil
import sys
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from agentablate import __version__

EVALUATOR_SCHEMA_VERSION = 2


def evaluator_environment_identity() -> dict[str, object]:
    """Return portable fields that define the evaluator execution environment."""
    return {
        "agentablate_version": __version__,
        "evaluator_schema_version": EVALUATOR_SCHEMA_VERSION,
        "platform_machine": platform.machine(),
        "platform_release": platform.release(),
        "platform_system": platform.system(),
        "platform_version": platform.version(),
        "python_implementation": platform.python_implementation(),
        "python_version": sys.version,
    }


def _normalize_distribution_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _distribution_snapshot(distributions: Iterable[Any]) -> str:
    packages = sorted(
        (
            _normalize_distribution_name(distribution.metadata["Name"]),
            distribution.version,
        )
        for distribution in distributions
        if distribution.metadata.get("Name")
    )
    encoded = json.dumps(packages, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _executable_sha256(path: str) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _resolve_executable(command: tuple[str, ...]) -> Path | None:
    executable = sys.executable if command[0] == "{python}" else command[0]
    resolved = shutil.which(executable)
    if resolved is None and Path(executable).is_file():
        resolved = executable
    return Path(resolved).resolve() if resolved else None


def _module_distribution(command: tuple[str, ...]) -> dict[str, str] | None:
    if len(command) < 3 or command[1] != "-m":
        return None
    module = command[2].split(".", 1)[0]
    names = importlib.metadata.packages_distributions().get(module, ())
    for name in sorted(names):
        try:
            return {
                "name": _normalize_distribution_name(name),
                "version": importlib.metadata.version(name),
            }
        except importlib.metadata.PackageNotFoundError:
            continue
    return None


def evaluator_identity(
    command: tuple[str, ...],
    *,
    distributions: Callable[[], Iterable[Any]] | None = None,
) -> dict[str, object]:
    """Return portable evidence binding one task's actual evaluator runtime."""
    if not command:
        raise ValueError("evaluator command must not be empty")
    identity = evaluator_environment_identity()
    identity["distributions_sha256"] = _distribution_snapshot(
        (distributions or importlib.metadata.distributions)()
    )
    executable = _resolve_executable(command)
    if executable is None:
        identity.update(
            executable_basename=Path(command[0]).name,
            executable_sha256="missing",
        )
    else:
        identity.update(
            executable_basename=executable.name,
            executable_sha256=_executable_sha256(str(executable)),
        )
    module_distribution = _module_distribution(command)
    if module_distribution is not None:
        identity["module_distribution"] = module_distribution
    return identity


def evaluator_environment_hash(identity: dict[str, object]) -> str:
    encoded = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()
