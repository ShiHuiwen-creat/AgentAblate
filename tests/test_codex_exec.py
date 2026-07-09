import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentablate.adapters.base import AdapterCancelled, AdapterResult, AdapterTimeout
from agentablate.adapters.codex_exec import (
    CODEX_EXEC_POLICY,
    CodexExecAdapter,
    CodexRuntimeChanged,
    codex_allowed_environment,
    discover_codex_runtime,
    verify_codex_runtime,
)
from agentablate.models import (
    AdapterRuntimeIdentity,
    AgentConfig,
    TaskSpec,
    TrialSpec,
    VariantConfig,
)
from agentablate.processes import minimal_environment


def _version(stdout: str = " codex-cli\t0.142.3 \n") -> SimpleNamespace:
    return SimpleNamespace(stdout=stdout)


def _runtime(executable: Path, *, ambient: str | None = None) -> AdapterRuntimeIdentity:
    return AdapterRuntimeIdentity(
        schema_version=1,
        executable=executable,
        executable_basename=executable.name,
        executable_sha256=hashlib.sha256(executable.read_bytes()).hexdigest(),
        version="codex-cli 0.test",
        policy=CODEX_EXEC_POLICY,
        ambient_skills_sha256=ambient or hashlib.sha256(b"[]").hexdigest(),
    )


def _trial(tmp_path: Path, prompt: str, timeout: int = 10) -> TrialSpec:
    return TrialSpec(
        id="native",
        experiment="demo",
        agent=AgentConfig(id="codex", adapter="codex-exec"),
        variant=VariantConfig(id="baseline"),
        task=TaskSpec(id="task", repo=tmp_path, prompt=prompt, test_command=("true",)),
        repetition=0,
        timeout_seconds=timeout,
        config_hash="hash",
        extension_hashes=(),
    )


@pytest.mark.asyncio
async def test_native_adapter_uses_literal_prompt_policy_worktree_and_preserves_jsonl(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "fake codex"
    executable.write_text(
        f"#!{Path(sys.executable).resolve()}\n"
        "import json,sys\n"
        "print(json.dumps({'argv':sys.argv[1:]}))\n"
    )
    executable.chmod(0o755)
    monkeypatch.setattr("agentablate.adapters.codex_exec._ambient_skill_paths", lambda: ())
    marker = tmp_path / "injected"
    prompt = f"literal {{prompt}} ; touch {marker} $(echo nope)"
    trial = _trial(tmp_path, prompt)
    adapter = CodexExecAdapter(_runtime(executable))

    assert adapter.command_for(trial, tmp_path) == (
        str(executable),
        "exec",
        *CODEX_EXEC_POLICY,
        "-C",
        str(tmp_path),
        prompt,
    )
    result = await adapter.run(trial, tmp_path)

    assert result.exit_code == 0
    assert json.loads(result.stdout)["argv"] == list(adapter.command_for(trial, tmp_path)[1:])
    assert not marker.exists()
    assert [event.kind for event in result.events] == ["start", "completed"]


@pytest.mark.asyncio
async def test_native_adapter_rejects_tampered_policy_before_process_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "codex"
    executable.write_bytes(b"fake")
    runtime = _runtime(executable).model_copy(update={"policy": ("--dangerous",)})
    launched = False

    async def create_process(*args, **kwargs):
        nonlocal launched
        launched = True
        raise AssertionError("must not launch")

    monkeypatch.setattr("agentablate.adapters.command.create_process", create_process)

    with pytest.raises(CodexRuntimeChanged, match="policy"):
        await CodexExecAdapter(runtime).run(_trial(tmp_path, "go"), tmp_path)

    assert not launched


@pytest.mark.asyncio
async def test_native_adapter_preserves_nonzero_exit_and_partial_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "codex"
    executable.write_text(
        f"#!{Path(sys.executable).resolve()}\n"
        "import sys\nprint('{\"partial\":true}')\nsys.exit(7)\n"
    )
    executable.chmod(0o755)
    monkeypatch.setattr("agentablate.adapters.codex_exec._ambient_skill_paths", lambda: ())

    result = await CodexExecAdapter(_runtime(executable)).run(_trial(tmp_path, "go"), tmp_path)

    assert (result.exit_code, result.stdout.strip()) == (7, '{"partial":true}')


@pytest.mark.asyncio
async def test_doctor_login_status_is_shell_free_minimal_and_redacted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "codex"
    executable.write_bytes(b"fake")
    observed: dict[str, object] = {}

    class Process:
        returncode = 1

    async def create(command, *, cwd=None, env=None):
        observed.update(command=command, cwd=cwd, env=env)
        return Process()

    async def communicate(process, timeout):
        return b"account: private@example.test", b"secret-token"

    monkeypatch.setattr("agentablate.adapters.codex_exec.create_process", create)
    monkeypatch.setattr("agentablate.adapters.codex_exec.communicate", communicate)
    monkeypatch.setattr(
        "agentablate.adapters.codex_exec.minimal_environment",
        lambda allowed: {"SAFE": "1"},
    )

    available, detail = await CodexExecAdapter(_runtime(executable)).doctor()

    assert observed == {
        "command": (str(executable), "login", "status"),
        "cwd": None,
        "env": {"SAFE": "1"},
    }
    assert not available
    assert detail == "Codex authentication unavailable. Run codex login to authenticate."
    assert "private" not in detail and "secret" not in detail


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [TimeoutError(), OSError("account detail")])
async def test_doctor_failure_is_generic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: Exception
) -> None:
    executable = tmp_path / "codex"
    executable.write_bytes(b"fake")

    async def create(*args, **kwargs):
        raise failure

    monkeypatch.setattr("agentablate.adapters.codex_exec.create_process", create)
    assert await CodexExecAdapter(_runtime(executable)).doctor() == (
        False,
        "Codex authentication unavailable. Run codex login to authenticate.",
    )


@pytest.mark.asyncio
async def test_doctor_success_reports_runtime_and_ambient_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "codex"
    executable.write_bytes(b"fake")

    class Process:
        returncode = 0

    async def create(*args, **kwargs):
        return Process()

    async def communicate(*args):
        return b"account detail", b"secret"

    monkeypatch.setattr("agentablate.adapters.codex_exec.create_process", create)
    monkeypatch.setattr("agentablate.adapters.codex_exec.communicate", communicate)
    runtime = _runtime(executable, ambient="f" * 64)

    available, detail = await CodexExecAdapter(runtime).doctor()

    assert available
    assert detail == "codex codex-cli 0.test is available; warning: ambient skills are present"
    assert "account detail" not in detail and "secret" not in detail


@pytest.mark.asyncio
@pytest.mark.parametrize("exception_type", [AdapterTimeout, AdapterCancelled])
async def test_native_adapter_preserves_command_process_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    exception_type: type[BaseException],
) -> None:
    executable = tmp_path / "codex"
    executable.write_bytes(b"fake")
    monkeypatch.setattr("agentablate.adapters.codex_exec.verify_codex_runtime", lambda _: None)
    partial = AdapterResult(-1, (), "partial", "diagnostic")
    terminal = exception_type(partial)
    observed: dict[str, object] = {}

    class FakeCommandAdapter:
        def __init__(self, command, *, allowed_env, interpolate_prompt):
            observed.update(
                command=command,
                allowed_env=allowed_env,
                interpolate_prompt=interpolate_prompt,
            )

        async def run(self, trial, cwd, *, on_event=None):
            observed.update(trial=trial, cwd=cwd, on_event=on_event)
            raise terminal

    monkeypatch.setattr("agentablate.adapters.codex_exec.CommandAdapter", FakeCommandAdapter)
    trial = _trial(tmp_path, "go")

    def sink(event):
        return None

    with pytest.raises(exception_type) as raised:
        await CodexExecAdapter(_runtime(executable)).run(trial, tmp_path, on_event=sink)

    assert raised.value is terminal
    assert observed["command"][-1] == "go"
    assert observed["interpolate_prompt"] is False
    assert observed["trial"] is trial and observed["on_event"] is sink


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
        path.relative_to(skills).as_posix() for path in reads if path.is_relative_to(skills)
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
