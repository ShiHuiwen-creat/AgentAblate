import hashlib
from pathlib import Path
from typing import Any

import yaml

from agentablate.models import ExperimentBundle, ExperimentConfig, TaskSpec


class ConfigurationError(ValueError):
    """Raised when experiment YAML cannot be parsed or validated."""


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as error:
        raise ConfigurationError(f"invalid YAML in {path}: {error}") from error
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a YAML mapping")
    return data


def load_experiment(path: Path) -> ExperimentBundle:
    source = path.resolve()
    base = source.parent
    raw = _read_yaml(source)
    raw["tasks"] = [(base / item).resolve() for item in raw.get("tasks", [])]
    for variant in raw.get("variants", []):
        variant["skills"] = [
            (base / item).resolve() for item in variant.get("skills", [])
        ]
        variant["mcp"] = [
            (base / item).resolve() for item in variant.get("mcp", [])
        ]
    config = ExperimentConfig.model_validate(raw)

    tasks: list[TaskSpec] = []
    for task_path in config.tasks:
        task_raw = _read_yaml(task_path)
        task_raw["repo"] = (task_path.parent / task_raw["repo"]).resolve()
        tasks.append(TaskSpec.model_validate(task_raw))
    config_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    return ExperimentBundle(
        source=source,
        config_hash=config_hash,
        config=config,
        tasks=tasks,
    )
