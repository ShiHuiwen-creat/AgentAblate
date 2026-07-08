from pathlib import Path
from types import SimpleNamespace

import pytest

from agentablate.adapters.codex_exec import (
    CODEX_EXEC_POLICY,
    CodexRuntimeChanged,
    codex_allowed_environment,
    discover_codex_runtime,
    verify_codex_runtime,
)
from agentablate.processes import minimal_environment


def _version(stdout: str = " codex-cli\t0.142.3 \n") -> SimpleNamespace:
    return SimpleNamespace(stdout=stdout)


def test_discovery_prefers_path_and_normalizes_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "bin" / "codex"
    executable.parent.mkdir()
    executable.write_bytes(b"path-codex")
    desktop = tmp_path / "Codex.app" / "codex"
    desktop.parent.mkdir()
    desktop.write_bytes(b"desktop-codex")
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr("agentablate.adapters.codex_exec.shutil.which", lambda _: str(executable))
    monkeypatch.setattr("agentablate.adapters.codex_exec.platform.system", lambda: "Darwin")
    monkeypatch.setattr("agentablate.adapters.codex_exec._MACOS_CODEX_CANDIDATES", (desktop,))
    monkeypatch.setattr("agentablate.adapters.codex_exec._ambient_skill_paths", lambda: ())

    def run(command: tuple[str, ...], **kwargs: object) -> SimpleNamespace:
        calls.append(command)
        assert kwargs == {"capture_output": True, "check": True, "text": True}
        return _version()

    monkeypatch.setattr("agentablate.adapters.codex_exec.subprocess.run", run)
    identity = discover_codex_runtime()

    assert identity.executable == executable.resolve()
    assert identity.executable_basename == "codex"
    assert identity.version == "codex-cli 0.142.3"
    assert identity.policy == CODEX_EXEC_POLICY
    assert calls == [(str(executable.resolve()), "--version")]


def test_macos_discovery_uses_desktop_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    desktop = tmp_path / "Codex.app" / "codex"
    desktop.parent.mkdir()
    desktop.write_bytes(b"desktop-codex")
    monkeypatch.setattr("agentablate.adapters.codex_exec.shutil.which", lambda _: None)
    monkeypatch.setattr("agentablate.adapters.codex_exec.platform.system", lambda: "Darwin")
    monkeypatch.setattr("agentablate.adapters.codex_exec._MACOS_CODEX_CANDIDATES", (desktop,))
    monkeypatch.setattr("agentablate.adapters.codex_exec._ambient_skill_paths", lambda: ())
    monkeypatch.setattr(
        "agentablate.adapters.codex_exec.subprocess.run", lambda *a, **k: _version()
    )

    assert discover_codex_runtime().executable == desktop.resolve()


def test_windows_discovery_uses_path_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    desktop = tmp_path / "codex"
    desktop.write_bytes(b"desktop")
    monkeypatch.setattr("agentablate.adapters.codex_exec.shutil.which", lambda _: None)
    monkeypatch.setattr("agentablate.adapters.codex_exec.platform.system", lambda: "Windows")
    monkeypatch.setattr("agentablate.adapters.codex_exec._MACOS_CODEX_CANDIDATES", (desktop,))

    with pytest.raises(FileNotFoundError, match="Codex CLI"):
        discover_codex_runtime()


def test_discovery_reports_missing_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("agentablate.adapters.codex_exec.shutil.which", lambda _: None)
    monkeypatch.setattr("agentablate.adapters.codex_exec.platform.system", lambda: "Linux")
    with pytest.raises(FileNotFoundError, match="Codex CLI"):
        discover_codex_runtime()


def test_verify_detects_executable_byte_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "codex"
    executable.write_bytes(b"first")
    monkeypatch.setattr("agentablate.adapters.codex_exec.shutil.which", lambda _: str(executable))
    monkeypatch.setattr("agentablate.adapters.codex_exec.platform.system", lambda: "Linux")
    monkeypatch.setattr("agentablate.adapters.codex_exec._ambient_skill_paths", lambda: ())
    monkeypatch.setattr(
        "agentablate.adapters.codex_exec.subprocess.run", lambda *a, **k: _version()
    )
    identity = discover_codex_runtime()
    executable.write_bytes(b"second")

    with pytest.raises(CodexRuntimeChanged, match="executable"):
        verify_codex_runtime(identity)


def test_verify_detects_ambient_skill_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "codex"
    executable.write_bytes(b"codex")
    skills = tmp_path / ".agents" / "skills"
    skill = skills / "reviewer"
    skill.mkdir(parents=True)
    document = skill / "SKILL.md"
    document.write_text("instructions")
    monkeypatch.setattr("agentablate.adapters.codex_exec.shutil.which", lambda _: str(executable))
    monkeypatch.setattr("agentablate.adapters.codex_exec.platform.system", lambda: "Linux")
    monkeypatch.setattr("agentablate.adapters.codex_exec._ambient_skill_paths", lambda: (skills,))
    monkeypatch.setattr(
        "agentablate.adapters.codex_exec.subprocess.run", lambda *a, **k: _version()
    )
    identity = discover_codex_runtime()
    document.write_text("changed")

    with pytest.raises(CodexRuntimeChanged, match="ambient"):
        verify_codex_runtime(identity)


def test_ambient_fingerprint_skips_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "codex"
    executable.write_bytes(b"codex")
    skills = tmp_path / "skills"
    skills.mkdir()
    credential = skills / "auth.json"
    credential.write_text('{"token":"first"}')
    monkeypatch.setattr("agentablate.adapters.codex_exec.shutil.which", lambda _: str(executable))
    monkeypatch.setattr("agentablate.adapters.codex_exec.platform.system", lambda: "Linux")
    monkeypatch.setattr("agentablate.adapters.codex_exec._ambient_skill_paths", lambda: (skills,))
    monkeypatch.setattr(
        "agentablate.adapters.codex_exec.subprocess.run", lambda *a, **k: _version()
    )
    first = discover_codex_runtime()
    credential.write_text('{"token":"second"}')

    assert discover_codex_runtime().ambient_skills_sha256 == first.ambient_skills_sha256


def test_ambient_fingerprint_never_reads_credential_shaped_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "codex"
    executable.write_bytes(b"codex")
    skills = tmp_path / "skills"
    sensitive = (
        ".env",
        ".env.local",
        "token.json",
        "tokens.json",
        "secret.json",
        "secrets.json",
        "auth.json",
        "credential.json",
        "credentials.json",
        "client.pem",
        "private.key",
        "identity.p12",
        "archive.pfx",
        ".ssh/id_rsa",
        ".aws/credentials",
        ".gnupg/private-keys-v1.d/key",
    )
    for relative in sensitive:
        path = skills / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(f"do-not-read:{relative}".encode())
    safe = skills / "reviewer" / "SKILL.md"
    safe.parent.mkdir()
    safe.write_text("safe instructions")
    original_read_bytes = Path.read_bytes
    reads: list[Path] = []

    def read_bytes(path: Path) -> bytes:
        reads.append(path)
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    monkeypatch.setattr("agentablate.adapters.codex_exec.shutil.which", lambda _: str(executable))
    monkeypatch.setattr("agentablate.adapters.codex_exec.platform.system", lambda: "Linux")
    monkeypatch.setattr("agentablate.adapters.codex_exec._ambient_skill_paths", lambda: (skills,))
    monkeypatch.setattr(
        "agentablate.adapters.codex_exec.subprocess.run", lambda *a, **k: _version()
    )

    discover_codex_runtime()

    relative_reads = {
        path.relative_to(skills).as_posix()
        for path in reads
        if path.is_relative_to(skills)
    }
    assert relative_reads == {"reviewer/SKILL.md"}


def test_windows_allowed_environment_is_exact_and_credential_free(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("agentablate.adapters.codex_exec.platform.system", lambda: "Windows")
    allowed = codex_allowed_environment()

    assert allowed == ("SystemRoot", "ComSpec", "USERPROFILE", "LOCALAPPDATA", "TEMP", "TMP")
    assert not any(
        fragment in name.upper() for name in allowed for fragment in ("TOKEN", "KEY", "SECRET")
    )


def test_windows_allowed_environment_integrates_with_minimal_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("agentablate.adapters.codex_exec.platform.system", lambda: "Windows")
    names = (
        "PATH",
        "HOME",
        "TMPDIR",
        "SystemRoot",
        "ComSpec",
        "USERPROFILE",
        "LOCALAPPDATA",
        "TEMP",
        "TMP",
        "CODEX_TOKEN",
        "API_KEY",
        "CLIENT_SECRET",
    )
    monkeypatch.setattr("agentablate.processes.os.environ", {name: name for name in names})

    environment = minimal_environment(codex_allowed_environment())

    assert tuple(environment) == names[:9]
    assert not {"CODEX_TOKEN", "API_KEY", "CLIENT_SECRET"} & environment.keys()


def test_posix_allowed_environment_adds_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("agentablate.adapters.codex_exec.platform.system", lambda: "Linux")
    assert codex_allowed_environment() == ()
