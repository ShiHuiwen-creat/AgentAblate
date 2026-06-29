import asyncio
import subprocess
from pathlib import Path
from types import TracebackType


class WorkspaceError(RuntimeError):
    """Raised when Git cannot create or clean up an isolated workspace."""

    def __init__(self, command: tuple[str, ...], stderr: str) -> None:
        self.command = command
        self.stderr = stderr
        detail = stderr.strip() or "git command failed"
        super().__init__(f"{' '.join(command)}: {detail}")


def resolve_revision(repo: Path, revision: str) -> str:
    """Resolve a revision to the immutable commit object it names."""
    command = ("git", "-C", str(repo), "rev-parse", "--verify", f"{revision}^{{commit}}")
    completed = subprocess.run(command, capture_output=True, text=True)
    if completed.returncode != 0:
        raise WorkspaceError(command, completed.stderr)
    return completed.stdout.strip()


async def _run_git(*args: str) -> str:
    command = ("git", *args)
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    if process.returncode != 0:
        raise WorkspaceError(command, stderr.decode(errors="replace"))
    return stdout.decode(errors="replace").strip()


class WorktreeWorkspace:
    """A detached, disposable Git worktree for a single trial."""

    def __init__(self, repo: Path, revision: str, path: Path) -> None:
        self.repo = repo.resolve()
        self.requested_revision = revision
        self.path = path.resolve()
        self.revision = ""
        self._created = False

    async def __aenter__(self) -> "WorktreeWorkspace":
        inside = await _run_git("-C", str(self.repo), "rev-parse", "--is-inside-work-tree")
        if inside != "true":
            command = ("git", "-C", str(self.repo), "rev-parse", "--is-inside-work-tree")
            raise WorkspaceError(command, f"{self.repo} is not a git worktree")

        self.revision = await _run_git(
            "-C",
            str(self.repo),
            "rev-parse",
            "--verify",
            f"{self.requested_revision}^{{commit}}",
        )
        await _run_git(
            "-C",
            str(self.repo),
            "worktree",
            "add",
            "--detach",
            str(self.path),
            self.revision,
        )
        self._created = True
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if not self._created:
            return
        try:
            await _run_git(
                "-C", str(self.repo), "worktree", "remove", "--force", str(self.path)
            )
        finally:
            await _run_git("-C", str(self.repo), "worktree", "prune")
