import hashlib
import json
from pathlib import Path

from agentablate.models import ExperimentBundle, TrialSpec


def fingerprint_path(path: Path) -> str:
    if path.is_file():
        payload: object = {
            "kind": "file",
            "name": path.name,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
    elif path.is_dir():
        children = sorted(item for item in path.rglob("*") if item.is_file())
        payload = {
            "kind": "directory",
            "files": [
                (
                    child.relative_to(path).as_posix(),
                    hashlib.sha256(child.read_bytes()).hexdigest(),
                )
                for child in children
            ],
        }
    else:
        payload = {"kind": "missing", "name": path.name}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _trial_id(payload: dict[str, object]) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()[:16]


def expand_matrix(bundle: ExperimentBundle) -> list[TrialSpec]:
    trials: list[TrialSpec] = []
    meta = bundle.config.experiment
    for task in bundle.tasks:
        for agent in bundle.config.agents:
            for variant in bundle.config.variants:
                skill_hashes = tuple(
                    fingerprint_path(path) for path in variant.skills
                )
                mcp_hashes = tuple(fingerprint_path(path) for path in variant.mcp)
                extension_hashes = (*skill_hashes, *mcp_hashes)
                for repetition in range(meta.repetitions):
                    payload = {
                        "config_hash": bundle.config_hash,
                        "experiment": meta.name,
                        "agent": agent.model_dump(mode="json"),
                        "variant": {
                            "id": variant.id,
                            "skill_hashes": skill_hashes,
                            "mcp_hashes": mcp_hashes,
                        },
                        "task": {
                            "id": task.id,
                            "revision": task.revision,
                            "prompt": task.prompt,
                            "test_command": task.test_command,
                        },
                        "repetition": repetition,
                    }
                    trials.append(
                        TrialSpec(
                            id=_trial_id(payload),
                            experiment=meta.name,
                            agent=agent,
                            variant=variant,
                            task=task,
                            repetition=repetition,
                            timeout_seconds=meta.timeout_seconds,
                            config_hash=bundle.config_hash,
                            extension_hashes=extension_hashes,
                        )
                    )
    return trials
