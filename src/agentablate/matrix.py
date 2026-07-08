import hashlib
import json
from pathlib import Path

from agentablate.adapters.codex_exec import discover_codex_runtime
from agentablate.identity import evaluator_environment_hash, evaluator_identity
from agentablate.models import ExperimentBundle, TrialSpec
from agentablate.skills import fingerprint_tree, inspect_skills
from agentablate.workspace import resolve_revision


def fingerprint_path(path: Path) -> str:
    if path.is_file():
        payload: object = {
            "kind": "file",
            "name": path.name,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
    elif path.is_dir():
        return fingerprint_tree(path)
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
    runtimes = {
        agent.id: discover_codex_runtime()
        for agent in bundle.config.agents
        if agent.adapter == "codex-exec"
    }
    variants = {}
    for variant in bundle.config.variants:
        skill_trees = inspect_skills(variant.skills)
        skill_hashes = tuple(tree.identity.fingerprint for tree in skill_trees)
        mcp_hashes = tuple(fingerprint_path(path) for path in variant.mcp)
        variants[variant.id] = (
            tuple(tree.identity for tree in skill_trees),
            skill_hashes,
            mcp_hashes,
        )
    for task in bundle.tasks:
        evaluator_hash = evaluator_environment_hash(
            evaluator_identity(task.test_command)
        )
        resolved_task = task.model_copy(
            update={"revision": resolve_revision(task.repo, task.revision)}
        )
        for agent in bundle.config.agents:
            for variant in bundle.config.variants:
                skill_inputs, skill_hashes, mcp_hashes = variants[variant.id]
                extension_hashes = (*skill_hashes, *mcp_hashes)
                runtime = runtimes.get(agent.id)
                for repetition in range(meta.repetitions):
                    payload: dict[str, object] = {
                        "config_hash": bundle.config_hash,
                        "experiment": meta.name,
                        "agent": agent.model_dump(mode="json"),
                        "variant": {
                            "id": variant.id,
                            "skill_hashes": skill_hashes,
                            "mcp_hashes": mcp_hashes,
                        },
                        "task": {
                            "id": resolved_task.id,
                            "revision": resolved_task.revision,
                            "prompt": resolved_task.prompt,
                            "test_command": resolved_task.test_command,
                        },
                        "repetition": repetition,
                        "evaluator_hash": evaluator_hash,
                    }
                    if runtime is not None:
                        payload["adapter_runtime"] = runtime.model_dump(mode="json")
                        payload["skill_inputs"] = [
                            identity.model_dump(mode="json")
                            for identity in skill_inputs
                        ]
                    trials.append(
                        TrialSpec(
                            id=_trial_id(payload),
                            experiment=meta.name,
                            agent=agent,
                            variant=variant,
                            task=resolved_task,
                            repetition=repetition,
                            timeout_seconds=meta.timeout_seconds,
                            config_hash=bundle.config_hash,
                            extension_hashes=extension_hashes,
                            evaluator_hash=evaluator_hash,
                            adapter_runtime=runtime,
                            skill_inputs=skill_inputs,
                        )
                    )
    return trials
