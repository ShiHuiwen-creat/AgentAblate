from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True)


class ExperimentMeta(FrozenModel):
    name: str
    repetitions: int = Field(default=1, ge=1)
    timeout_seconds: int = Field(default=900, ge=1)


class AgentConfig(FrozenModel):
    id: str
    adapter: Literal[
        "fake",
        "command",
        "codex-sdk",
        "codex-exec",
        "codex-app-server",
        "claude-code",
        "gemini-cli",
    ]
    command: tuple[str, ...] | None = None


class VariantConfig(FrozenModel):
    id: str
    skills: tuple[Path, ...] = ()
    mcp: tuple[Path, ...] = ()


class TaskSpec(FrozenModel):
    id: str
    repo: Path
    revision: str = "HEAD"
    prompt: str
    test_command: tuple[str, ...]


class ExperimentConfig(FrozenModel):
    version: Literal[1]
    experiment: ExperimentMeta
    agents: tuple[AgentConfig, ...]
    variants: tuple[VariantConfig, ...]
    tasks: tuple[Path, ...]

    @model_validator(mode="after")
    def unique_ids(self) -> "ExperimentConfig":
        for label, values in (("agent", self.agents), ("variant", self.variants)):
            ids = [value.id for value in values]
            if len(ids) != len(set(ids)):
                raise ValueError(f"duplicate {label} id")
        return self


class ExperimentBundle(FrozenModel):
    source: Path
    config_hash: str
    config: ExperimentConfig
    tasks: tuple[TaskSpec, ...]


class TrialSpec(FrozenModel):
    id: str
    experiment: str
    agent: AgentConfig
    variant: VariantConfig
    task: TaskSpec
    repetition: int
    timeout_seconds: int
    config_hash: str
    extension_hashes: tuple[str, ...]
