# Codex Exec Adapter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a safe, reproducible first-party `codex-exec` adapter and make Codex skill variants execute inside isolated trial worktrees.

**Architecture:** A dedicated adapter discovers and freezes one Codex CLI runtime, then delegates process lifecycle behavior to the proven `CommandAdapter`. A focused skill module validates and fingerprints skill trees, installs them only around agent execution, and restores the worktree before evaluation. Matrix expansion binds both Codex runtime and ambient user skills into each trial ID.

**Tech Stack:** Python 3.11+, asyncio subprocesses, Pydantic v2, PyYAML, pytest/pytest-asyncio, Ruff, Hatchling.

## Global Constraints

- Never invoke Codex through a shell.
- Always use `--json --color never --sandbox workspace-write --ephemeral --ignore-user-config`.
- Never use `danger-full-access`, bypass approvals, copy credentials, or persist secrets.
- Use `PATH`, `HOME`, and `TMPDIR` on POSIX; on Windows also allow only
  `SystemRoot`, `ComSpec`, `USERPROFILE`, `LOCALAPPDATA`, `TEMP`, and `TMP`.
- Reject skill symlinks and all non-regular special files.
- Bind executable bytes, CLI version, effective flags, and ambient user skills into trial identity.
- Recheck frozen executable and ambient-skill fingerprints immediately before execution.
- Fail before Codex starts when a `codex-exec` trial declares MCP inputs.
- Keep live, billed Codex runs out of automated tests.
- Preserve Python 3.11, 3.12, 3.13, macOS/Linux/Windows support.

---

### Task 1: Safe skill metadata and fingerprints

**Files:**
- Create: `src/agentablate/skills.py`
- Modify: `src/agentablate/models.py`
- Modify: `src/agentablate/matrix.py`
- Test: `tests/test_skills.py`
- Test: `tests/test_matrix.py`

**Interfaces:**
- Produces: `SkillInputIdentity(name: str, fingerprint: str, install_name: str)` in
  `models.py` and `SkillTree(identity: SkillInputIdentity, source: Path)`.
- Produces: `inspect_skill(path: Path) -> SkillTree` and `fingerprint_tree(path: Path) -> str`.
- Consumed by: Tasks 2 and 4 for ambient identity and worktree installation.

- [ ] **Step 1: Write failing validation and deterministic fingerprint tests**

Add tests that create a valid `SKILL.md` frontmatter document, assert `install_name == "reviewer-<12 hash chars>"`, and assert fingerprints change when file bytes or executable bits change. Add parameterized tests rejecting a missing `SKILL.md`, duplicate names via `inspect_skills`, a symlink used as the skill root, a nested symlink, FIFO where supported, and names outside `^[a-z0-9]+(?:-[a-z0-9]+)*$`.

```python
def test_skill_fingerprint_binds_content_and_mode(tmp_path: Path) -> None:
    skill = write_skill(tmp_path / "skill", "reviewer")
    first = inspect_skill(skill)
    script = skill / "scripts" / "run.sh"
    script.parent.mkdir()
    script.write_text("exit 0\n")
    script.chmod(0o755)
    executable = inspect_skill(skill)
    script.chmod(0o644)
    non_executable = inspect_skill(skill)
    assert first.identity.fingerprint != executable.identity.fingerprint
    assert executable.identity.fingerprint != non_executable.identity.fingerprint
```

- [ ] **Step 2: Run the focused tests and confirm RED**

Run: `.venv/bin/python -m pytest tests/test_skills.py tests/test_matrix.py -q`

Expected: collection fails because `agentablate.skills` does not exist.

- [ ] **Step 3: Implement strict tree inspection**

Call `os.lstat(path)` on the skill root before any `resolve()` call. Implement a
frozen dataclass and Pydantic identity model. Walk with `os.scandir`, use
`entry.stat(follow_symlinks=False)`, reject `stat.S_ISLNK` and anything other than
directories or regular files, and hash JSON records containing relative POSIX path,
SHA-256 bytes, and `mode & 0o111`. Parse only the YAML frontmatter at the start of
`SKILL.md`, validate `name`, and reject duplicate names in `inspect_skills(paths)`.

```python
class SkillInputIdentity(FrozenModel):
    name: str
    fingerprint: str
    install_name: str

@dataclass(frozen=True, slots=True)
class SkillTree:
    identity: SkillInputIdentity
    source: Path

def inspect_skill(path: Path) -> SkillTree:
    _require_real_directory(os.lstat(path), path)
    source = path.resolve(strict=True)
    records = _regular_file_records(source)
    name = _frontmatter_name(source / "SKILL.md")
    fingerprint = _records_hash(records)
    identity = SkillInputIdentity(
        name=name,
        fingerprint=fingerprint,
        install_name=f"{name}-{fingerprint[:12]}",
    )
    return SkillTree(identity, source)
```

Change `matrix.fingerprint_path()` to call the strict tree fingerprint for directories while retaining its existing regular-file behavior. This makes permission changes part of existing extension hashes without changing file-only inputs.

- [ ] **Step 4: Run focused tests and confirm GREEN**

Run: `.venv/bin/python -m pytest tests/test_skills.py tests/test_matrix.py -q`

Expected: all focused tests pass.

- [ ] **Step 5: Run Ruff and commit**

Run: `.venv/bin/python -m ruff check src/agentablate/skills.py src/agentablate/matrix.py tests/test_skills.py tests/test_matrix.py`

Commit:

```bash
git add src/agentablate/skills.py src/agentablate/models.py src/agentablate/matrix.py tests/test_skills.py tests/test_matrix.py
git commit -m "feat: validate reproducible skill inputs"
```

---

### Task 2: Freeze Codex runtime identity into trials

**Files:**
- Create: `src/agentablate/adapters/codex_exec.py`
- Modify: `src/agentablate/models.py`
- Modify: `src/agentablate/matrix.py`
- Modify: `src/agentablate/storage.py`
- Test: `tests/test_codex_exec.py`
- Test: `tests/test_matrix.py`
- Test: `tests/test_storage.py`

**Interfaces:**
- Produces: `AdapterRuntimeIdentity` Pydantic model in `models.py`.
- Produces: `discover_codex_runtime() -> AdapterRuntimeIdentity`.
- Produces: `verify_codex_runtime(identity: AdapterRuntimeIdentity) -> None`.
- Consumed by: Task 3's `CodexExecAdapter`.

- [ ] **Step 1: Write failing discovery and identity tests**

Use monkeypatches for `shutil.which`, `platform.system`, `subprocess.run`, and candidate paths. Cover PATH precedence, the macOS Desktop path `/Applications/Codex.app/Contents/Resources/codex`, missing CLI, normalized `codex-cli 0.142.3` output, executable-byte changes, ambient user-skill changes, and trial-ID changes. Add storage tests asserting canonical runtime JSON is written and read, non-Codex trials store `{}`, and opening a Phase 1 database adds the column without losing rows.
Also cover Windows PATH discovery and a platform-minimal environment containing
exactly the allowed Windows launch/profile variables while excluding token, key,
and secret variables.

```python
def test_codex_runtime_changes_trial_id(bundle, monkeypatch) -> None:
    monkeypatch.setattr(
        "agentablate.matrix.discover_codex_runtime",
        lambda: AdapterRuntimeIdentity(
            schema_version=1,
            executable="/tools/codex",
            executable_basename="codex",
            executable_sha256="a" * 64,
            version="codex-cli 1.0.0",
            policy=CODEX_EXEC_POLICY,
            ambient_skills_sha256="b" * 64,
        ),
    )
    first = expand_matrix(codex_bundle(bundle))[0]
    monkeypatch.setattr(
        "agentablate.matrix.discover_codex_runtime",
        lambda: first.adapter_runtime.model_copy(
            update={"executable_sha256": "c" * 64}
        ),
    )
    second = expand_matrix(codex_bundle(bundle))[0]
    assert first.id != second.id
```

- [ ] **Step 2: Run focused tests and confirm RED**

Run: `.venv/bin/python -m pytest tests/test_codex_exec.py tests/test_matrix.py tests/test_storage.py -q`

Expected: imports or assertions fail because runtime identity is absent.

- [ ] **Step 3: Add the frozen runtime model and discovery functions**

Add this model and a nullable field on `TrialSpec`:

```python
class AdapterRuntimeIdentity(FrozenModel):
    schema_version: Literal[1]
    executable: Path
    executable_basename: str
    executable_sha256: str
    version: str
    policy: tuple[str, ...]
    ambient_skills_sha256: str

class TrialSpec(FrozenModel):
    # existing fields remain unchanged
    adapter_runtime: AdapterRuntimeIdentity | None = None
    skill_inputs: tuple[SkillInputIdentity, ...] = ()
```

In `codex_exec.py`, define one immutable `CODEX_EXEC_POLICY` tuple, discover an executable using PATH then the macOS Desktop candidate, run `(path, "--version")` without a shell, normalize whitespace, and fingerprint ambient skill directories with Task 1's strict tree walker while explicitly skipping credential files. Windows uses PATH only. Add `codex_allowed_environment() -> tuple[str, ...]` returning the extra Windows names from Global Constraints and an empty tuple on POSIX; `CommandAdapter` still supplies `PATH`, `HOME`, and `TMPDIR`. Do not reuse an unrestricted parent environment.

`verify_codex_runtime` must hash the frozen absolute executable and recompute ambient skills, raising `CodexRuntimeChanged` on mismatch.

- [ ] **Step 4: Bind runtime identity during matrix expansion**

Resolve once per configured Codex agent. Inspect each variant's skills once and
store ordered `SkillInputIdentity` values on `TrialSpec`. Include both
`runtime.model_dump(mode="json")` and the structured skill identities in the
trial-ID payload. Non-Codex agents retain `adapter_runtime=None`; all agents receive
structured skill identities while preserving the existing ordered extension hashes.

```python
runtimes = {
    agent.id: discover_codex_runtime()
    for agent in bundle.config.agents
    if agent.adapter == "codex-exec"
}
```

Add `adapter_runtime_json TEXT NOT NULL DEFAULT '{}'` to the trial table and to the
existing additive migration map in `SQLiteStorage`. Serialize
`trial.adapter_runtime.model_dump(mode="json")` with sorted compact JSON when
claiming/registering a trial, return the parsed object from `get_trial`, and retain
`{}` for non-Codex and legacy rows. Never store executable output beyond normalized
version or any authentication-status text.

- [ ] **Step 5: Run focused tests, Ruff, and commit**

Run:

```bash
.venv/bin/python -m pytest tests/test_codex_exec.py tests/test_matrix.py tests/test_storage.py -q
.venv/bin/python -m ruff check src/agentablate/adapters/codex_exec.py src/agentablate/models.py src/agentablate/matrix.py src/agentablate/storage.py tests/test_codex_exec.py tests/test_matrix.py tests/test_storage.py
```

Expected: both commands exit 0.

Commit:

```bash
git add src/agentablate/adapters/codex_exec.py src/agentablate/models.py src/agentablate/matrix.py src/agentablate/storage.py tests/test_codex_exec.py tests/test_matrix.py tests/test_storage.py
git commit -m "feat: bind Codex runtime identity"
```

---

### Task 3: Execute Codex safely through the native adapter

**Files:**
- Modify: `src/agentablate/adapters/codex_exec.py`
- Modify: `src/agentablate/experiment.py`
- Modify: `src/agentablate/runner.py`
- Test: `tests/test_codex_exec.py`
- Test: `tests/test_experiment.py`
- Test: `tests/test_runner.py`

**Interfaces:**
- Consumes: `AdapterRuntimeIdentity`, `verify_codex_runtime`.
- Produces: `CodexExecAdapter(runtime: AdapterRuntimeIdentity)` implementing `AgentAdapter`.
- Produces: doctor and runner registry support for adapter key `codex-exec`.

- [ ] **Step 1: Write failing argv, process, doctor, and registry tests**

Use a fake executable that records `sys.argv` and prints JSONL. Assert the prompt remains one literal argument, the exact safety flags are present, `-C` points to the worktree, JSONL remains in stdout, and shell metacharacters do not execute. Reuse parameterized CommandAdapter behavior tests or inject a fake `CommandAdapter` to cover non-zero exit, timeout, cancellation, partial output, terminal events, and event-sink failure without network access.

```python
expected = (
    str(runtime.executable), "exec", "--json", "--color", "never",
    "--sandbox", "workspace-write", "--ephemeral", "--ignore-user-config",
    "-C", str(tmp_path), trial.task.prompt,
)
assert adapter.command_for(trial, tmp_path) == expected
```

Add tests proving `experiment._adapter` and `TrialRunner` both select the native adapter. Doctor must run the exact shell-free command `(frozen_executable, "login", "status")` with the platform-minimal environment. Exit 0 reports basename/version plus any ambient-skill warning. Non-zero, timeout, or launch failure returns unavailable with the generic instruction `Run codex login to authenticate.`; command stdout/stderr and possible account details are neither returned nor persisted.

- [ ] **Step 2: Run focused tests and confirm RED**

Run: `.venv/bin/python -m pytest tests/test_codex_exec.py tests/test_experiment.py tests/test_runner.py -q`

Expected: new adapter behavior and registry assertions fail.

- [ ] **Step 3: Implement minimal adapter using CommandAdapter composition**

```python
class CodexExecAdapter:
    def __init__(self, runtime: AdapterRuntimeIdentity) -> None:
        self.runtime = runtime

    async def doctor(self) -> tuple[bool, str]:
        status = await _login_status(self.runtime.executable)
        if not status:
            return False, "Codex authentication unavailable. Run codex login to authenticate."
        return True, _doctor_message(self.runtime)

    def command_for(self, trial: TrialSpec, cwd: Path) -> tuple[str, ...]:
        return (
            str(self.runtime.executable), "exec", *CODEX_EXEC_POLICY,
            "-C", str(cwd), _prompt(trial),
        )

    async def run(self, trial, cwd, *, on_event=None) -> AdapterResult:
        verify_codex_runtime(self.runtime)
        return await CommandAdapter(
            self.command_for(trial, cwd),
            allowed_env=codex_allowed_environment(),
        ).run(
            trial, cwd, on_event=on_event
        )
```

Build `_prompt` from the task prompt only in this task; Task 4 adds explicit skill invocations. Register the same runtime-aware constructor in both doctor and runner paths. Reject a missing `trial.adapter_runtime` with `AdapterConfigurationError`.

- [ ] **Step 4: Run focused tests, Ruff, and commit**

Run:

```bash
.venv/bin/python -m pytest tests/test_codex_exec.py tests/test_experiment.py tests/test_runner.py -q
.venv/bin/python -m ruff check src tests
```

Expected: both commands exit 0.

Commit:

```bash
git add src/agentablate/adapters/codex_exec.py src/agentablate/experiment.py src/agentablate/runner.py tests/test_codex_exec.py tests/test_experiment.py tests/test_runner.py
git commit -m "feat: run native Codex CLI trials"
```

---

### Task 4: Install skills only during Codex execution

**Files:**
- Modify: `src/agentablate/skills.py`
- Modify: `src/agentablate/adapters/codex_exec.py`
- Modify: `src/agentablate/runner.py`
- Test: `tests/test_skills.py`
- Test: `tests/test_codex_exec.py`
- Test: `tests/test_runner.py`

**Interfaces:**
- Produces: `installed_skills(paths, frozen_identities, workspace) -> ContextManager[tuple[SkillTree, ...]]`.
- Consumes: returned skill names in `CodexExecAdapter.command_for`.
- Runner guarantees the context exits before invoking the evaluator.

- [ ] **Step 1: Write failing lifecycle and MCP tests**

Test installation at `.agents/skills/<install_name>`, byte/mode preservation, restoration of a pre-existing target, cleanup after success/non-zero/timeout/cancellation, evaluator observing no injected files, and two concurrent worktrees staying isolated. Reject `.agents`, `.agents/skills`, the existing target itself, or any item inside a pre-existing target tree when it is a symlink or special file; assert backup never reads through it and no write escapes the worktree. Simulate source mutation before copying and during copying, require the installed-snapshot fingerprint mismatch to clean up, and assert Codex never starts. Inject `OSError` midway through copying after a pre-existing target was backed up; assert its bytes/modes are fully restored and no partial target remains.

```python
with installed_skills((skill,), trial.skill_inputs, workspace) as installed:
    target = workspace / ".agents" / "skills" / installed[0].identity.install_name
    assert (target / "SKILL.md").is_file()
assert not target.exists()
```

Add a runner test asserting a non-empty `trial.variant.mcp` raises `AdapterConfigurationError` before the adapter factory is called. Add a command test asserting skill prompts begin with exact `$<frontmatter-name>` invocations followed by the unchanged task prompt. Add a double-failure test where execution raises `AdapterCancelled` and restoration raises `OSError`; assert the resulting `BaseExceptionGroup` retains both objects and cancellation is not replaced.

- [ ] **Step 2: Run focused tests and confirm RED**

Run: `.venv/bin/python -m pytest tests/test_skills.py tests/test_codex_exec.py tests/test_runner.py -q`

Expected: installation, cleanup, invocation, and MCP assertions fail.

- [ ] **Step 3: Implement transactional installation**

Use a synchronous context manager because file copies are local and bounded.
Pair each `variant.skills` path with the same-index `trial.skill_inputs` identity;
reject length, name, install-name, or fingerprint mismatches. Before creating a
destination, walk from the resolved worktree root through `.agents`, `skills`, and
the target with `lstat`, rejecting symlinks and special files and verifying every
resolved component remains under the worktree. Place backups in a unique
`tempfile.TemporaryDirectory` outside the worktree, copy with
`shutil.copy2(..., follow_symlinks=False)`, then strictly inspect and fingerprint
the installed snapshot. Compare it to the frozen `SkillInputIdentity`; on mismatch,
restore and fail before yielding.

`_install_and_verify` is itself transactional because it runs before the context
body exists. It creates a state journal before the first mutation, validates the
complete pre-existing target tree with the same strict no-link/no-special-file
walker, and records each successful backup, directory creation, and copy. Wrap all
installation operations in `try/except BaseException`; on any error call
`_restore(state)` in reverse order. If rollback also fails, raise a
`BaseExceptionGroup` containing installation and rollback errors. Only return state
after all installed snapshots match their frozen identities.

Do not rely on a plain generator `finally` for dual failures. Capture
`BaseException` from the body, run restoration, and when restoration also fails
raise `BaseExceptionGroup("skill execution and restoration failed", [primary,
cleanup])`. When only the primary failed, re-raise it with its original traceback;
when only restoration failed, raise restoration. This applies to
`asyncio.CancelledError` as well as ordinary exceptions.

```python
@contextmanager
def installed_skills(paths, frozen_identities, workspace):
    installed = _validate_frozen_inputs(paths, frozen_identities)
    state = _install_and_verify(installed, workspace)
    primary = None
    try:
        yield installed
    except BaseException as error:
        primary = error
    try:
        _restore(state)
    except BaseException as cleanup:
        if primary is not None:
            raise BaseExceptionGroup(
                "skill execution and restoration failed", [primary, cleanup]
            ) from None
        raise
    if primary is not None:
        raise primary.with_traceback(primary.__traceback__)
```

In `TrialRunner._execute_trial`, enter this context only for `codex-exec`, execute the adapter, exit the context, then run the evaluator. Reject MCP before entering the workspace. Extend `_prompt` to prepend `Use these skills for this task: $name ...` only when skills exist.

- [ ] **Step 4: Run focused tests, full tests, Ruff, and commit**

Run:

```bash
.venv/bin/python -m pytest tests/test_skills.py tests/test_codex_exec.py tests/test_runner.py -q
.venv/bin/python -m pytest -q
.venv/bin/python -m ruff check .
```

Expected: all commands exit 0.

Commit:

```bash
git add src/agentablate/skills.py src/agentablate/adapters/codex_exec.py src/agentablate/runner.py tests/test_skills.py tests/test_codex_exec.py tests/test_runner.py
git commit -m "feat: isolate Codex skill variants"
```

---

### Task 5: Documentation, example, and release verification

**Files:**
- Modify: `README.md`
- Modify: `CONTRIBUTING.md`
- Create: `examples/codex-skill-ablation/agentablate.yaml`
- Create: `examples/codex-skill-ablation/task.yaml`
- Create: `examples/codex-skill-ablation/fixture/README.md`
- Create: `examples/codex-skill-ablation/skill/SKILL.md`
- Modify: `pyproject.toml`
- Modify: `tests/test_example.py`
- Modify: `.github/workflows/ci.yml` only if packaging tests reveal a missing file assertion.

**Interfaces:**
- Documents the final CLI/config contract and a manual smoke test.
- Adds a packaged, network-free validation of the example's structure.

- [ ] **Step 1: Write the failing packaged-example test**

Assert all example files are included in the source distribution, YAML validates,
fixture initialization works, the skill passes `inspect_skill`, and replacing the
Codex executable with a fake produces one completed trial without network or
billing. The wheel contains only the library and templates; examples are not wheel
runtime data.

```python
def test_codex_example_is_packaged_and_valid(tmp_path: Path) -> None:
    example = project_root / "examples" / "codex-skill-ablation"
    bundle = load_experiment(example / "agentablate.yaml")
    assert bundle.config.agents[0].adapter == "codex-exec"
    assert inspect_skill(bundle.config.variants[1].skills[0]).identity.name
```

- [ ] **Step 2: Run the example test and confirm RED**

Run: `.venv/bin/python -m pytest tests/test_example.py -q`

Expected: failure because the Codex example does not exist.

- [ ] **Step 3: Add concise user documentation and example**

Update README availability and roadmap, document installation/login, Desktop CLI discovery, sandbox policy, ambient-skill evidence, cost/network warning, unsupported MCP fail-fast behavior, and commands for `doctor`, filtered `run`, `compare`, and `report`. Add a small baseline-versus-skill fixture whose evaluator checks only the requested code change. Document that a live run consumes Codex quota and is manual.

Update CONTRIBUTING with the fake-executable rule for adapter tests and the prohibition on credentials in fixtures, logs, or CI. Add an explicit Hatch sdist include for `examples/codex-skill-ablation/**` in `pyproject.toml`, then inspect the built `.tar.gz` in the test to prove those paths ship.

- [ ] **Step 4: Run complete verification**

Run:

```bash
.venv/bin/python -m ruff check .
.venv/bin/python -m pytest --cov=agentablate --cov-report=term-missing
.venv/bin/python -m build
git diff --check
```

Expected: Ruff exits 0; all tests pass with total coverage at least 90%; sdist and wheel build; diff check exits 0.

- [ ] **Step 5: Commit, push, and create a draft PR**

```bash
git add README.md CONTRIBUTING.md pyproject.toml examples/codex-skill-ablation tests/test_example.py .github/workflows/ci.yml
git commit -m "docs: add Codex skill ablation quickstart"
git push -u origin feat/codex-exec-adapter
```

Create a draft PR targeting `main` summarizing runtime identity, safety boundaries, skill isolation, unsupported MCP behavior, and the exact verification results. Do not mark ready or merge until GitHub CI and review both pass.
