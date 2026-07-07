import hashlib
import json
import platform
import sys

from agentablate import __version__

EVALUATOR_SCHEMA_VERSION = 1


def evaluator_environment_identity() -> dict[str, object]:
    """Return portable fields that define the evaluator execution environment."""
    return {
        "agentablate_version": __version__,
        "evaluator_schema_version": EVALUATOR_SCHEMA_VERSION,
        "platform_machine": platform.machine(),
        "platform_system": platform.system(),
        "python_implementation": platform.python_implementation(),
        "python_version": sys.version,
    }


def evaluator_environment_hash(identity: dict[str, object]) -> str:
    encoded = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()
