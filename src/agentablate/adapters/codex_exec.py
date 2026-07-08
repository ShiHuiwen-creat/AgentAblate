import hashlib
import json
import os
import platform
import shutil
import stat
import subprocess
from contextlib import suppress
from pathlib import Path

from agentablate.adapters.base import AdapterResult, EventSink
from agentablate.adapters.command import CommandAdapter
from agentablate.models import AdapterRuntimeIdentity, TrialSpec
from agentablate.processes import (
    ProcessCancelled,
    ProcessTimeout,
    communicate,
    create_process,
    minimal_environment,
    terminate_process,
)

CODEX_EXEC_POLICY = (
    "--json",
    "--color",
    "never",
    "--sandbox",
    "workspace-write",
    "--ephemeral",
    "--ignore-user-config",
)

_MACOS_CODEX_CANDIDATES = (Path("/Applications/Codex.app/Contents/Resources/codex"),)
_CREDENTIAL_DIRECTORIES = frozenset({".ssh", ".aws", ".gnupg"})
_CREDENTIAL_FILES = frozenset(
    {
        "auth.json",
        "credential.json",
        "credentials.json",
        "secret.json",
        "secrets.json",
        "token.json",
        "tokens.json",
    }
)
_CREDENTIAL_SUFFIXES = frozenset({".pem", ".key", ".p12", ".pfx"})
_WINDOWS_ENVIRONMENT = (
    "SystemRoot",
    "ComSpec",
    "USERPROFILE",
    "LOCALAPPDATA",
    "TEMP",
    "TMP",
)


class CodexRuntimeChanged(RuntimeError):
    """The frozen Codex runtime no longer matches the local runtime."""


_LOGIN_FAILURE = "Codex authentication unavailable. Run codex login to authenticate."
_EMPTY_AMBIENT_HASH = hashlib.sha256(b"[]").hexdigest()


class CodexExecAdapter:
    def __init__(self, runtime: AdapterRuntimeIdentity) -> None:
        self.runtime = runtime

    async def doctor(self) -> tuple[bool, str]:
        if not await _login_status(self.runtime.executable):
            return False, _LOGIN_FAILURE
        message = f"{self.runtime.executable_basename} {self.runtime.version} is available"
        if self.runtime.ambient_skills_sha256 != _EMPTY_AMBIENT_HASH:
            message += "; warning: ambient skills are present"
        return True, message

    def command_for(self, trial: TrialSpec, cwd: Path) -> tuple[str, ...]:
        return (
            str(self.runtime.executable),
            "exec",
            *CODEX_EXEC_POLICY,
            "-C",
            str(cwd),
            trial.task.prompt,
        )

    async def run(
        self, trial: TrialSpec, cwd: Path, *, on_event: EventSink | None = None
    ) -> AdapterResult:
        verify_codex_runtime(self.runtime)
        return await CommandAdapter(
            self.command_for(trial, cwd),
            allowed_env=codex_allowed_environment(),
        ).run(trial, cwd, on_event=on_event)


async def _login_status(executable: Path) -> bool:
    process = None
    try:
        process = await create_process(
            (str(executable), "login", "status"),
            cwd=None,
            env=minimal_environment(codex_allowed_environment()),
        )
        await communicate(process, 10)
        return process.returncode == 0
    except (OSError, TimeoutError, ProcessTimeout, ProcessCancelled):
        if process is not None:
            with suppress(BaseException):
                await terminate_process(process)
        return False


def codex_allowed_environment() -> tuple[str, ...]:
    return _WINDOWS_ENVIRONMENT if platform.system() == "Windows" else ()


def _ambient_skill_paths() -> tuple[Path, ...]:
    home = Path.home()
    return (home / ".agents" / "skills", home / ".codex" / "skills")


def _is_credential_path(path: Path, root: Path) -> bool:
    relative = path.relative_to(root)
    parts = tuple(part.lower() for part in relative.parts)
    name = parts[-1]
    return (
        bool(_CREDENTIAL_DIRECTORIES.intersection(parts))
        or name == ".env"
        or name.startswith(".env.")
        or name in _CREDENTIAL_FILES
        or path.suffix.lower() in _CREDENTIAL_SUFFIXES
    )


def _ambient_skills_hash() -> str:
    records: list[dict[str, object]] = []

    def visit(root_index: int, root: Path, directory: Path) -> None:
        with os.scandir(directory) as entries:
            for entry in sorted(entries, key=lambda item: item.name):
                path = Path(entry.path)
                if _is_credential_path(path, root):
                    continue
                metadata = entry.stat(follow_symlinks=False)
                if stat.S_ISLNK(metadata.st_mode):
                    raise ValueError(f"ambient skill tree must not contain a symlink: {path}")
                if stat.S_ISDIR(metadata.st_mode):
                    visit(root_index, root, path)
                elif stat.S_ISREG(metadata.st_mode):
                    records.append(
                        {
                            "root": root_index,
                            "path": path.relative_to(root).as_posix(),
                            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                            "mode": metadata.st_mode & 0o111,
                        }
                    )
                else:
                    raise ValueError(
                        f"ambient skill tree entries must be a regular file or directory: {path}"
                    )

    for index, candidate in enumerate(_ambient_skill_paths()):
        try:
            metadata = os.lstat(candidate)
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(metadata.st_mode):
            raise ValueError(f"ambient skill root must not be a symlink: {candidate}")
        if not stat.S_ISDIR(metadata.st_mode):
            raise ValueError(f"ambient skill root must be a directory: {candidate}")
        root = candidate.resolve(strict=True)
        visit(index, root, root)
    encoded = json.dumps(records, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _executable_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _discover_executable() -> Path:
    if found := shutil.which("codex"):
        return Path(found).resolve(strict=True)
    if platform.system() == "Darwin":
        for candidate in _MACOS_CODEX_CANDIDATES:
            if candidate.is_file():
                return candidate.resolve(strict=True)
    raise FileNotFoundError("Codex CLI executable was not found")


def discover_codex_runtime() -> AdapterRuntimeIdentity:
    executable = _discover_executable()
    completed = subprocess.run(
        (str(executable), "--version"),
        capture_output=True,
        check=True,
        text=True,
    )
    return AdapterRuntimeIdentity(
        schema_version=1,
        executable=executable,
        executable_basename=executable.name,
        executable_sha256=_executable_hash(executable),
        version=" ".join(completed.stdout.split()),
        policy=CODEX_EXEC_POLICY,
        ambient_skills_sha256=_ambient_skills_hash(),
    )


def verify_codex_runtime(identity: AdapterRuntimeIdentity) -> None:
    try:
        executable_hash = _executable_hash(identity.executable)
    except OSError as error:
        raise CodexRuntimeChanged("Codex executable changed") from error
    if executable_hash != identity.executable_sha256:
        raise CodexRuntimeChanged("Codex executable changed")
    if _ambient_skills_hash() != identity.ambient_skills_sha256:
        raise CodexRuntimeChanged("Codex ambient skills changed")
