from pathlib import Path

import pytest
from pydantic import ValidationError

from agentablate.config import load_experiment
from agentablate.models import VariantConfig


def test_load_experiment_resolves_task_files(tmp_path: Path) -> None:
    task = tmp_path / "task.yaml"
    task.write_text(
        """
id: fix-bug
repo: ./fixture
prompt: Fix the bug.
test_command: [python, -m, pytest, -q]
""".strip()
    )
    config = tmp_path / "agentablate.yaml"
    config.write_text(
        """
version: 1
experiment:
  name: demo
  repetitions: 2
agents:
  - id: fake
    adapter: fake
variants:
  - id: baseline
  - id: with-skill
    skills: [./skill]
tasks: [./task.yaml]
""".strip()
    )

    bundle = load_experiment(config)

    assert bundle.config.experiment.name == "demo"
    assert bundle.tasks[0].repo == (tmp_path / "fixture").resolve()
    assert bundle.config.variants[1].skills == ((tmp_path / "skill").resolve(),)


def test_nested_configuration_is_immutable(tmp_path: Path) -> None:
    variant = VariantConfig(id="baseline")

    with pytest.raises(AttributeError):
        variant.skills.append(tmp_path)  # type: ignore[attr-defined]


def test_rejects_duplicate_ids(tmp_path: Path) -> None:
    config = tmp_path / "agentablate.yaml"
    config.write_text(
        """
version: 1
experiment: {name: demo}
agents:
  - {id: fake, adapter: fake}
  - {id: fake, adapter: fake}
variants: [{id: baseline}]
tasks: []
""".strip()
    )

    with pytest.raises(ValidationError, match="duplicate agent id"):
        load_experiment(config)
