import asyncio
import subprocess
from pathlib import Path
from types import TracebackType

from agentablate.processes import communicate, create_process


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
    process = await create_process(command)
    stdout, stderr = await communicate(process)
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
        try:
            await _run_git(
                "-C",
                str(self.repo),
                "worktree",
                "add",
                "--detach",
                str(self.path),
                self.revision,
            )
        except BaseException as error:
            cleanup_errors = await self._finish_cleanup(force_remove=False)
            if cleanup_errors:
                raise BaseExceptionGroup(
                    "worktree creation and cleanup failed", [error, *cleanup_errors]
                ) from None
            raise
        self._created = True
        return self

    async def _cleanup(self, *, force_remove: bool) -> list[BaseException]:
        errors: list[BaseException] = []
        should_remove = force_remove
        if not force_remove:
            try:
                registrations = await _run_git(
                    "-C", str(self.repo), "worktree", "list", "--porcelain"
                )
                should_remove = f"worktree {self.path}" in registrations.splitlines()
            except BaseException as error:
                errors.append(error)
        if should_remove:
            try:
                await _run_git(
                    "-C", str(self.repo), "worktree", "remove", "--force", str(self.path)
                )
            except BaseException as error:
                errors.append(error)
        try:
            await _run_git("-C", str(self.repo), "worktree", "prune")
        except BaseException as error:
            errors.append(error)
        self._created = False
        return errors

    async def _finish_cleanup(self, *, force_remove: bool) -> list[BaseException]:
        cleanup = asyncio.create_task(self._cleanup(force_remove=force_remove))
        try:
            return await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            await cleanup
            raise

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if not self._created:
            return
        cleanup_errors = await self._finish_cleanup(force_remove=True)
        if not cleanup_errors:
            return
        if exc_value is not None:
            raise BaseExceptionGroup(
                "trial and workspace cleanup failed", [exc_value, *cleanup_errors]
            ) from None
        if len(cleanup_errors) == 1:
            raise cleanup_errors[0]
        raise BaseExceptionGroup("workspace cleanup failed", cleanup_errors)
