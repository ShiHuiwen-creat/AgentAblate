import os
from pathlib import Path

from agentablate.identity import evaluator_environment_hash, evaluator_identity


class Distribution:
    def __init__(self, name: str, version: str) -> None:
        self.metadata = {"Name": name}
        self.version = version


def test_evaluator_identity_binds_executable_content(
    tmp_path: Path, monkeypatch
) -> None:
    first = tmp_path / "first" / "checker"
    second = tmp_path / "second" / "checker"
    first.parent.mkdir()
    second.parent.mkdir()
    first.write_bytes(b"version one")
    second.write_bytes(b"version two")

    monkeypatch.setattr("agentablate.identity.shutil.which", lambda name: str(first))
    first_identity = evaluator_identity(("checker",))
    monkeypatch.setattr("agentablate.identity.shutil.which", lambda name: str(second))
    second_identity = evaluator_identity(("checker",))

    assert first_identity["executable_basename"] == "checker"
    assert first_identity["executable_sha256"] != second_identity["executable_sha256"]
    assert str(tmp_path) not in repr(first_identity) + repr(second_identity)


def test_executable_identity_detects_same_size_content_with_restored_mtime(
    tmp_path: Path, monkeypatch
) -> None:
    executable = tmp_path / "checker"
    executable.write_bytes(b"content-one")
    original_stat = executable.stat()
    monkeypatch.setattr(
        "agentablate.identity.shutil.which", lambda name: str(executable)
    )
    before = evaluator_identity(("checker",))

    executable.write_bytes(b"content-two")
    os.utime(
        executable,
        ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
    )
    after = evaluator_identity(("checker",))

    assert before["executable_sha256"] != after["executable_sha256"]


def test_evaluator_identity_binds_normalized_dependency_snapshot() -> None:
    first = evaluator_identity(
        ("missing-command",),
        distributions=lambda: [Distribution("Example_Pkg", "1.0")],
    )
    second = evaluator_identity(
        ("missing-command",),
        distributions=lambda: [Distribution("example-pkg", "2.0")],
    )

    assert first["distributions_sha256"] != second["distributions_sha256"]
    assert "Example_Pkg" not in repr(first)
    assert evaluator_environment_hash(first) != evaluator_environment_hash(second)


def test_default_dependency_snapshot_refreshes_between_calls(monkeypatch) -> None:
    monkeypatch.setattr(
        "agentablate.identity.importlib.metadata.distributions",
        lambda: [Distribution("changing-package", "1.0")],
    )
    before = evaluator_identity(("missing-command",))
    monkeypatch.setattr(
        "agentablate.identity.importlib.metadata.distributions",
        lambda: [Distribution("changing-package", "2.0")],
    )
    after = evaluator_identity(("missing-command",))

    assert before["distributions_sha256"] != after["distributions_sha256"]
