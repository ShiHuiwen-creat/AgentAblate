import asyncio
import subprocess
from pathlib import Path

import pytest

import agentablate.workspace as workspace_module
from agentablate.workspace import WorkspaceError, WorktreeWorkspace


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ("git", *args),
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture
def fixture_repo(tmp_path: Path) -> tuple[Path, str, str]:
    repo = tmp_path / "fixture"
    repo.mkdir()
    _git("init", cwd=repo)
    _git("config", "user.name", "AgentAblate Tests", cwd=repo)
    _git("config", "user.email", "tests@example.com", cwd=repo)
    tracked = repo / "tracked.txt"
    tracked.write_text("first\n")
    _git("add", "tracked.txt", cwd=repo)
    _git("commit", "-m", "first", cwd=repo)
    first = _git("rev-parse", "HEAD", cwd=repo)
    tracked.write_text("second\n")
    _git("commit", "-am", "second", cwd=repo)
    second = _git("rev-parse", "HEAD", cwd=repo)
    return repo, first, second


@pytest.mark.asyncio
async def test_detached_worktree_starts_at_requested_revision(
    fixture_repo: tuple[Path, str, str], tmp_path: Path
) -> None:
    repo, first, _ = fixture_repo
    trial_dir = tmp_path / "trial"

    async with WorktreeWorkspace(repo, first, trial_dir) as workspace:
        assert workspace.path == trial_dir
        assert workspace.revision == first
        assert _git("rev-parse", "HEAD", cwd=trial_dir) == first
        assert _git("branch", "--show-current", cwd=trial_dir) == ""


@pytest.mark.asyncio
async def test_trial_edits_do_not_change_fixture_repo(
    fixture_repo: tuple[Path, str, str], tmp_path: Path
) -> None:
    repo, _, second = fixture_repo

    async with WorktreeWorkspace(repo, second, tmp_path / "trial") as workspace:
        (workspace.path / "tracked.txt").write_text("trial edit\n")
        (workspace.path / "new.txt").write_text("new\n")

        assert (repo / "tracked.txt").read_text() == "second\n"
        assert not (repo / "new.txt").exists()


@pytest.mark.asyncio
async def test_cleanup_removes_worktree_registration(
    fixture_repo: tuple[Path, str, str], tmp_path: Path
) -> None:
    repo, _, second = fixture_repo
    trial_dir = tmp_path / "trial"

    async with WorktreeWorkspace(repo, second, trial_dir):
        assert str(trial_dir) in _git("worktree", "list", "--porcelain", cwd=repo)

    assert str(trial_dir) not in _git("worktree", "list", "--porcelain", cwd=repo)
    assert not trial_dir.exists()


@pytest.mark.asyncio
async def test_cleanup_runs_when_trial_body_raises(
    fixture_repo: tuple[Path, str, str], tmp_path: Path
) -> None:
    repo, _, second = fixture_repo
    trial_dir = tmp_path / "trial"

    with pytest.raises(RuntimeError, match="trial failed"):
        async with WorktreeWorkspace(repo, second, trial_dir):
            raise RuntimeError("trial failed")

    assert str(trial_dir) not in _git("worktree", "list", "--porcelain", cwd=repo)


@pytest.mark.asyncio
async def test_cancelling_creation_after_add_cleans_registration(
    fixture_repo: tuple[Path, str, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, _, second = fixture_repo
    trial_dir = tmp_path / "trial"
    added = asyncio.Event()
    original_run_git = workspace_module._run_git

    async def pause_after_add(*args: str) -> str:
        result = await original_run_git(*args)
        if "add" in args:
            added.set()
            await asyncio.Event().wait()
        return result

    monkeypatch.setattr(workspace_module, "_run_git", pause_after_add)

    async def enter_workspace() -> None:
        async with WorktreeWorkspace(repo, second, trial_dir):
            pass

    running = asyncio.create_task(enter_workspace())
    await asyncio.wait_for(added.wait(), timeout=2)
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running

    registrations = await original_run_git(
        "-C", str(repo), "worktree", "list", "--porcelain"
    )
    assert str(trial_dir) not in registrations


@pytest.mark.asyncio
async def test_cleanup_errors_are_grouped_with_trial_error(
    fixture_repo: tuple[Path, str, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, _, second = fixture_repo
    original_run_git = workspace_module._run_git
    workspace = WorktreeWorkspace(repo, second, tmp_path / "trial")

    with pytest.raises(BaseExceptionGroup) as captured:
        async with workspace:
            async def fail_cleanup(*args: str) -> str:
                if "remove" in args or "prune" in args:
                    raise WorkspaceError(("git", *args), "cleanup failed")
                return await original_run_git(*args)

            monkeypatch.setattr(workspace_module, "_run_git", fail_cleanup)
            raise RuntimeError("trial failed")

    grouped = repr(captured.value.exceptions)
    assert "trial failed" in grouped
    assert "cleanup failed" in grouped


@pytest.mark.asyncio
async def test_invalid_revision_raises_typed_error_with_git_stderr(
    fixture_repo: tuple[Path, str, str], tmp_path: Path
) -> None:
    repo, _, _ = fixture_repo

    with pytest.raises(WorkspaceError) as captured:
        async with WorktreeWorkspace(repo, "missing-revision", tmp_path / "trial"):
            pass

    assert "Needed a single revision" in captured.value.stderr
    assert captured.value.stderr.strip() in str(captured.value)


@pytest.mark.asyncio
async def test_non_git_repository_raises_typed_error_with_git_stderr(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "not-a-repo"
    repo.mkdir()

    with pytest.raises(WorkspaceError, match="not a git repository") as captured:
        async with WorktreeWorkspace(repo, "HEAD", tmp_path / "trial"):
            pass

    assert captured.value.stderr.strip() in str(captured.value)
