# Deterministic Skill Impact Demo Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a reproducible offline demo in which the baseline fails, the skill-enabled variant succeeds, and AgentAblate reports a `+100.0 pp` success-rate delta.

**Architecture:** A fixture-local Python command acts as an explicitly labelled deterministic demo agent. The command adapter resolves an exact `{python}` argument to `sys.executable`, and the trial runner applies the existing transactional skill lifecycle to every adapter. The demo agent reacts only to a validated skill installed under `.agents/skills`; evaluation begins after skill cleanup.

**Tech Stack:** Python 3.11+, Typer CLI tests, YAML experiment configuration, Git fixture worktrees, pytest, Ruff, Markdown.

## Global Constraints

- The demo must run offline and require no API key.
- The expected result is `baseline` 0%, `with-skill` 100%, and `+100.0 pp`.
- The demo must be explicitly labelled deterministic and must not claim Codex or model performance.
- Do not add dependencies or demo-specific branching to production adapters; exact `{python}` resolution and adapter-independent skill installation must be general capabilities.
- Preserve the live Codex example as the real-agent follow-up path.
- Support Python 3.11 and newer on the project's existing platforms.

## File Structure

- `examples/skill-impact-demo/agentablate.yaml`: two-variant command-agent experiment.
- `examples/skill-impact-demo/task.yaml`: exact artifact evaluator.
- `examples/skill-impact-demo/skill/SKILL.md`: the treatment instruction.
- `examples/skill-impact-demo/fixture/demo_agent.py`: deterministic agent behavior.
- `examples/skill-impact-demo/fixture/README.md`: committed fixture seed.
- `src/agentablate/adapters/command.py`: portable exact `{python}` resolution in doctor and run paths.
- `tests/test_adapters.py`: command-adapter placeholder contract.
- `src/agentablate/runner.py`: transactional skill installation around every adapter execution.
- `tests/test_runner.py`: adapter-independent skill visibility and cleanup contract.
- `tests/test_example.py`: end-to-end, cleanup, packaging, and documentation-drift coverage.
- `docs/skill-impact-demo.md`: checked-in representative comparison artifact.
- `README.md`: first-screen result and copyable walkthrough.

---

### Task 1: Build the End-to-End Deterministic Example

**Files:**
- Modify: `src/agentablate/adapters/command.py`
- Modify: `tests/test_adapters.py`
- Modify: `src/agentablate/runner.py`
- Modify: `tests/test_runner.py`
- Create: `examples/skill-impact-demo/agentablate.yaml`
- Create: `examples/skill-impact-demo/task.yaml`
- Create: `examples/skill-impact-demo/skill/SKILL.md`
- Create: `examples/skill-impact-demo/fixture/demo_agent.py`
- Create: `examples/skill-impact-demo/fixture/README.md`
- Modify: `tests/test_example.py`

**Interfaces:**
- Consumes: existing `command` adapter, `.agents/skills/<install-name>/SKILL.md` installation contract, `initialize_fixture_repository()`, and Typer `app`.
- Produces: portable command execution, adapter-independent transactional skill visibility, and a runnable example whose SQLite comparison contains one paired row for `deterministic-demo` and `with-skill` with `success_rate_delta == 1.0`.

- [ ] **Step 1: Write the failing command-adapter placeholder test**

Add to `tests/test_adapters.py`:

```python
@pytest.mark.asyncio
async def test_command_adapter_resolves_exact_python_placeholder(tmp_path: Path) -> None:
    adapter = CommandAdapter(
        ("{python}", "-c", "import sys; print(sys.executable)")
    )

    available, _ = await adapter.doctor()
    result = await adapter.run(_trial(), tmp_path)

    assert available is True
    assert Path(result.stdout.strip()).resolve() == Path(sys.executable).resolve()
```

- [ ] **Step 2: Run the placeholder test to verify it fails**

Run:

```bash
../codex-exec-adapter/.venv/bin/python -m pytest tests/test_adapters.py::test_command_adapter_resolves_exact_python_placeholder -q
```

Expected: FAIL because `doctor()` reports `{python}` missing or process launch raises `FileNotFoundError`.

- [ ] **Step 3: Implement exact portable interpreter resolution**

Import `sys` in `src/agentablate/adapters/command.py`. Resolve the executable in `doctor()`:

```python
executable = sys.executable if self.command[0] == "{python}" else self.command[0]
```

Build the runtime command in `run()` with exact-token handling before prompt interpolation:

```python
command = tuple(
    sys.executable
    if argument == "{python}"
    else (
        argument.replace("{prompt}", trial.task.prompt)
        if self.interpolate_prompt
        else argument
    )
    for argument in self.command
)
```

- [ ] **Step 4: Run the placeholder test to verify it passes**

Run:

```bash
../codex-exec-adapter/.venv/bin/python -m pytest tests/test_adapters.py::test_command_adapter_resolves_exact_python_placeholder -q
```

Expected: PASS.

- [ ] **Step 5: Write the failing adapter-independent skill lifecycle test**

Generalize `test_codex_skills_are_removed_before_evaluator` in `tests/test_runner.py` to use a command trial and rename it:

```python
@pytest.mark.asyncio
async def test_variant_skills_are_visible_to_adapter_and_removed_before_evaluator(
    tmp_path: Path,
) -> None:
    skill = tmp_path / "skill"
    skill.mkdir()
    (skill / "SKILL.md").write_text(
        "---\nname: reviewer\ndescription: Test\n---\n\nUse it.\n",
        encoding="utf-8",
    )
    from agentablate.skills import inspect_skill

    tree = inspect_skill(skill)
    trial = _trial(tmp_path, adapter="command").model_copy(
        update={
            "agent": AgentConfig(
                id="command", adapter="command", command=(sys.executable, "-c", "pass")
            ),
            "variant": VariantConfig(id="with-skill", skills=(skill,)),
            "skill_inputs": (tree.identity,),
        }
    )
```

Keep the existing observing adapter and evaluator assertions, register the observing adapter under `command`, and assert the skill exists during adapter execution but `.agents/skills` is absent during evaluation.

- [ ] **Step 6: Run the runner test to verify it fails**

Run:

```bash
../codex-exec-adapter/.venv/bin/python -m pytest tests/test_runner.py::test_variant_skills_are_visible_to_adapter_and_removed_before_evaluator -q
```

Expected: FAIL because the command adapter cannot observe the installed skill.

- [ ] **Step 7: Apply the transactional skill lifecycle to every adapter**

In `TrialRunner._execute_trial()`, preserve the Codex MCP rejection and replace the adapter-specific skill branch with:

```python
with installed_skills(trial.variant.skills, trial.skill_inputs, workspace.path):
    adapter_result, incremental = await self._run_adapter(
        trial, workspace.path, stdout_parts, stderr_parts, exit_codes
    )
```

The context must end before `_record_adapter_result()` and evaluator execution, preserving cleanup and failure composition behavior.

- [ ] **Step 8: Run the runner test to verify it passes**

Run:

```bash
../codex-exec-adapter/.venv/bin/python -m pytest tests/test_runner.py::test_variant_skills_are_visible_to_adapter_and_removed_before_evaluator -q
```

Expected: PASS.

- [ ] **Step 9: Write the failing end-to-end test**

Add these imports and test to `tests/test_example.py`:

```python
from agentablate.reporting import load_comparison, load_report


def test_skill_impact_demo_shows_deterministic_improvement(
    tmp_path: Path, monkeypatch
) -> None:
    source = PROJECT_ROOT / "examples" / "skill-impact-demo"
    example = tmp_path / "skill-impact-demo"
    shutil.copytree(source, example)
    initialize_fixture_repository(example / "fixture")
    subprocess.run(
        ["git", "-C", str(example / "fixture"), "add", "demo_agent.py"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(example / "fixture"), "commit", "--amend", "--no-edit"],
        check=True,
    )
    monkeypatch.chdir(example)

    runner = CliRunner()
    run_result = runner.invoke(app, ["run"])
    compare_result = runner.invoke(app, ["compare"])
    report_result = runner.invoke(app, ["report", "--format", "markdown"])

    assert run_result.exit_code == 0, run_result.output
    assert "Ran 2 trial(s); 0 failed" in run_result.output
    assert compare_result.exit_code == 0, compare_result.output
    assert "+100.0 pp" in compare_result.output
    assert report_result.exit_code == 0, report_result.output

    database = example / ".agentablate" / "results.sqlite3"
    report = load_report(database)
    comparison = load_comparison(database)
    assert [(row.variant_id, row.success_count) for row in report.rows] == [
        ("baseline", 0),
        ("with-skill", 1),
    ]
    assert len(comparison.rows) == 1
    assert comparison.rows[0].success_rate_delta == 1.0
    assert not (example / "fixture" / "skill-demo-output.txt").exists()
    fixture_status = subprocess.run(
        ["git", "-C", str(example / "fixture"), "status", "--porcelain"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert fixture_status.stdout == ""
```

- [ ] **Step 10: Run the test to verify the missing example fails**

Run:

```bash
../codex-exec-adapter/.venv/bin/python -m pytest tests/test_example.py::test_skill_impact_demo_shows_deterministic_improvement -q
```

Expected: FAIL because `examples/skill-impact-demo` does not exist or cannot be loaded.

- [ ] **Step 11: Add the minimal example configuration and task**

Create `examples/skill-impact-demo/agentablate.yaml`:

```yaml
version: 1
experiment:
  name: deterministic-skill-impact
  repetitions: 1
  timeout_seconds: 30
agents:
  - id: deterministic-demo
    adapter: command
    command: ["{python}", demo_agent.py]
variants:
  - id: baseline
  - id: with-skill
    skills:
      - ./skill
tasks:
  - ./task.yaml
```

Create `examples/skill-impact-demo/task.yaml`:

```yaml
id: create-skill-demo-output
repo: ./fixture
revision: HEAD
prompt: Create skill-demo-output.txt by following the available skill.
test_command:
  - "{python}"
  - -c
  - "from pathlib import Path; assert Path('skill-demo-output.txt').read_text(encoding='utf-8') == 'skill enabled\\n'"
```

- [ ] **Step 12: Add the deterministic skill and fixture agent**

Create `examples/skill-impact-demo/skill/SKILL.md`:

```markdown
---
name: skill-impact-demo
description: Demonstrate a deterministic skill-enabled outcome.
---

Create `skill-demo-output.txt` containing exactly `skill enabled` followed by a newline.
```

Create `examples/skill-impact-demo/fixture/demo_agent.py`:

```python
from pathlib import Path

EXPECTED = "Create `skill-demo-output.txt` containing exactly `skill enabled` followed by a newline."
skill_files = sorted(Path(".agents/skills").glob("*/SKILL.md"))
if len(skill_files) != 1:
    print("No single demo skill is installed; leaving the task unchanged.")
    raise SystemExit(0)

skill_text = skill_files[0].read_text(encoding="utf-8")
if EXPECTED not in skill_text:
    print("The installed skill does not contain the expected deterministic instruction.")
    raise SystemExit(2)

Path("skill-demo-output.txt").write_text("skill enabled\n", encoding="utf-8")
print("Applied the installed demo skill.")
```

Create `examples/skill-impact-demo/fixture/README.md`:

```markdown
# Deterministic skill impact fixture

This repository is copied into disposable trial worktrees by AgentAblate.
```

- [ ] **Step 13: Run the focused tests and lint the changed Python files**

Run:

```bash
../codex-exec-adapter/.venv/bin/python -m pytest tests/test_example.py::test_skill_impact_demo_shows_deterministic_improvement -q
../codex-exec-adapter/.venv/bin/python -m pytest tests/test_adapters.py::test_command_adapter_resolves_exact_python_placeholder -q
../codex-exec-adapter/.venv/bin/python -m pytest tests/test_runner.py::test_variant_skills_are_visible_to_adapter_and_removed_before_evaluator -q
../codex-exec-adapter/.venv/bin/python -m ruff check src/agentablate/adapters/command.py src/agentablate/runner.py tests/test_adapters.py tests/test_runner.py tests/test_example.py examples/skill-impact-demo/fixture/demo_agent.py
```

Expected: PASS; Ruff reports `All checks passed!`.

- [ ] **Step 14: Commit the portable command, skill lifecycle, and working example**

```bash
git add src/agentablate/adapters/command.py src/agentablate/runner.py tests/test_adapters.py tests/test_runner.py examples/skill-impact-demo tests/test_example.py
git commit -m "feat: add portable deterministic skill impact demo"
```

---

### Task 2: Add the Representative Result and Packaging Coverage

**Files:**
- Create: `docs/skill-impact-demo.md`
- Modify: `tests/test_example.py`

**Interfaces:**
- Consumes: the Task 1 example and CLI comparison output.
- Produces: a checked-in result whose table remains synchronized with generated output and whose example inputs are present in the source distribution.

- [ ] **Step 1: Extend the test with failing documentation and packaging assertions**

In `tests/test_example.py`, after generating `compare_result`, add:

```python
    checked_in_result = (PROJECT_ROOT / "docs" / "skill-impact-demo.md").read_text(
        encoding="utf-8"
    )
    expected_comparison_rows = [
        line for line in compare_result.output.splitlines() if line.startswith("|")
    ]
    for line in expected_comparison_rows:
        assert line in checked_in_result
```

Extend the existing source-distribution expected paths with:

```python
        "examples/skill-impact-demo/agentablate.yaml",
        "examples/skill-impact-demo/task.yaml",
        "examples/skill-impact-demo/fixture/README.md",
        "examples/skill-impact-demo/fixture/demo_agent.py",
        "examples/skill-impact-demo/skill/SKILL.md",
```

Rename the packaging test to `test_examples_are_packaged_and_valid` to reflect its wider scope.

- [ ] **Step 2: Run the focused tests to verify the result document is missing**

Run:

```bash
../codex-exec-adapter/.venv/bin/python -m pytest tests/test_example.py -q
```

Expected: FAIL with `FileNotFoundError` for `docs/skill-impact-demo.md`.

- [ ] **Step 3: Add the checked-in representative result**

Create `docs/skill-impact-demo.md`:

```markdown
# Deterministic skill impact demo

This is representative output from the offline deterministic demo. It verifies AgentAblate's experiment plumbing and does not measure Codex or another language model.

| Agent | Variant | Baseline (baseline) success | Variant success | Count delta | Rate delta |
|---|---|---:|---:|---:|---:|
| deterministic-demo | with-skill | 0/1 (0.0%) | 1/1 (100.0%) | +1 | +100.0 pp |
```

- [ ] **Step 4: Run example and packaging tests**

Run:

```bash
../codex-exec-adapter/.venv/bin/python -m pytest tests/test_example.py -q
```

Expected: all example tests pass and the source distribution contains both example trees.

- [ ] **Step 5: Commit the representative result**

```bash
git add docs/skill-impact-demo.md tests/test_example.py
git commit -m "test: verify skill demo result and packaging"
```

---

### Task 3: Put the Result in the README First Screen

**Files:**
- Modify: `README.md`

**Interfaces:**
- Consumes: the Task 1 example paths and Task 2 checked-in comparison.
- Produces: a concise first-screen walkthrough linking deterministic and live Codex examples.

- [ ] **Step 1: Add a failing README contract test**

Add to `tests/test_example.py`:

```python
def test_readme_features_skill_impact_demo() -> None:
    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")

    assert "## See a skill change the result" in readme
    assert "`baseline` | 0/1 (0%)" in readme
    assert "`with-skill` | 1/1 (100%)" in readme
    assert "examples/skill-impact-demo" in readme
    assert "docs/skill-impact-demo.md" in readme
    assert "examples/codex-skill-ablation" in readme
    assert "deterministic" in readme.lower()
```

- [ ] **Step 2: Run the README test to verify it fails**

Run:

```bash
../codex-exec-adapter/.venv/bin/python -m pytest tests/test_example.py::test_readme_features_skill_impact_demo -q
```

Expected: FAIL because the new heading and result table are absent.

- [ ] **Step 3: Replace the current sample-result section with the demo**

Replace `## See the result` and its existing table in `README.md` with:

```markdown
## See a skill change the result

The included offline deterministic demo runs the same task twice and changes only the skill input:

| Variant | Success |
|---|---:|
| `baseline` | 0/1 (0%) |
| `with-skill` | 1/1 (100%) |

That is a **+100.0 percentage-point** delta. It demonstrates AgentAblate's real skill-installation, evaluation, comparison, and reporting path; it is not a claim about model performance. See the [checked-in comparison](docs/skill-impact-demo.md).

```bash
git -C examples/skill-impact-demo/fixture init
git -C examples/skill-impact-demo/fixture config user.name AgentAblate
git -C examples/skill-impact-demo/fixture config user.email agentablate@example.invalid
git -C examples/skill-impact-demo/fixture add README.md demo_agent.py
git -C examples/skill-impact-demo/fixture commit -m "Initial fixture"
agentablate run examples/skill-impact-demo/agentablate.yaml
agentablate compare examples/skill-impact-demo/.agentablate/results.sqlite3
```

The `run` command completes successfully because both agent processes execute normally. The task-level `0% → 100%` difference is recorded in the report and comparison. For a live-agent follow-up, use the [Codex skill-ablation example](examples/codex-skill-ablation).
```

- [ ] **Step 4: Run the README contract and all example tests**

Run:

```bash
../codex-exec-adapter/.venv/bin/python -m pytest tests/test_example.py -q
```

Expected: all example tests pass.

- [ ] **Step 5: Commit the README presentation**

```bash
git add README.md tests/test_example.py
git commit -m "docs: feature deterministic skill impact demo"
```

---

### Task 4: Final Verification and Publish Preparation

**Files:**
- Verify only; modify a file only to fix a demonstrated failure.

**Interfaces:**
- Consumes: all previous task deliverables.
- Produces: a clean feature branch ready for review and a draft pull request.

- [ ] **Step 1: Run formatting and static checks**

```bash
../codex-exec-adapter/.venv/bin/python -m ruff check .
git diff --check origin/main...HEAD
```

Expected: Ruff reports `All checks passed!`; Git reports no whitespace errors.

- [ ] **Step 2: Run the full test suite**

```bash
../codex-exec-adapter/.venv/bin/python -m pytest -q
```

Expected: all tests pass with only the existing expected skip.

- [ ] **Step 3: Verify source and wheel builds**

```bash
../codex-exec-adapter/.venv/bin/python -m build --no-isolation
```

Expected: both `.tar.gz` and `.whl` artifacts build successfully.

- [ ] **Step 4: Review the final branch diff**

```bash
git status -sb
git diff --stat origin/main...HEAD
git log --oneline origin/main..HEAD
```

Expected: only the approved demo, tests, documentation, design, and plan are present; the worktree is clean.

- [ ] **Step 5: Push and open a draft PR**

```bash
git push -u origin feat/skill-impact-demo
gh pr create --draft --base main --head feat/skill-impact-demo --title "Feat: add deterministic skill impact demo" --body-file /tmp/agentablate-skill-demo-pr.md
```

Expected: GitHub returns the new draft pull-request URL.
