# Deterministic Skill Impact Demo Design

## Goal

Add a fast, offline demo that makes AgentAblate's core value visible: the same task fails without a skill and succeeds when the skill is enabled. The demo must exercise the real experiment, skill-installation, evaluation, storage, comparison, and reporting paths without claiming to measure a real model.

## Scope

The change adds a self-contained example under `examples/skill-impact-demo/`, checked-in representative comparison output, a focused README section, and automated coverage for the example. It also adds portable exact-argument `{python}` resolution to the general command adapter, matching the evaluator's existing contract. It does not add dependencies, publish model-performance claims, or replace the existing Codex example.

## Approach

Use the existing command adapter to run a small deterministic demo agent stored in the fixture repository. The example declares the executable as `{python}`; the command adapter resolves an argument exactly equal to that token to `sys.executable` in both `doctor()` and `run()`. Text that merely contains `{python}` is not modified. During a trial, the demo agent inspects the workspace for an installed skill. Without the skill it exits successfully but deliberately does not create the required artifact. With the skill it reads the installed instruction and creates the exact artifact expected by the task evaluator.

This keeps the distinction honest:

- the agent process is deterministic and explicitly labelled as a demo agent;
- AgentAblate's skill installation and cleanup are real;
- the task evaluator, result storage, comparison, and report generation are real;
- the result is a product walkthrough, not evidence about Codex or any other model.

Alternatives rejected:

- Changing `FakeAdapter` would mix presentation-specific behavior into core test infrastructure.
- Using only live Codex runs would require an account and network access and would not guarantee a stable first-run result.
- Checking in only a screenshot would be visually clear but would not be independently reproducible.

## Components

### Example configuration

`examples/skill-impact-demo/agentablate.yaml` defines one command agent, one task, and two variants:

- `baseline`, with no skills;
- `with-skill`, with the local demo skill.

The experiment uses one repetition and a short timeout so it completes quickly.

### Portable command interpreter

The command adapter accepts `{python}` as its executable token and resolves it to the interpreter running AgentAblate. This is a general command-adapter capability, not demo-specific branching, and keeps the example portable across virtual environments, macOS installations that expose only `python3`, and Windows interpreter paths.

### Fixture repository

The fixture contains a small Python demo agent and a README. The demo agent searches only `.agents/skills/*/SKILL.md` inside its disposable trial workspace. It creates `skill-demo-output.txt` only when the expected demo skill is installed.

The fixture must be initialized as a Git repository before the example runs, following the same pattern as the existing examples.

### Skill and task

The skill has valid frontmatter and gives one precise instruction: create `skill-demo-output.txt` with fixed content. The task's deterministic Python assertion checks that exact content.

### Representative result

A checked-in Markdown artifact shows the expected comparison:

| Variant | Success rate |
|---|---:|
| `baseline` | 0% |
| `with-skill` | 100% |

The comparison must show a `+100.0 pp` rate delta. Nearby text explicitly identifies the result as deterministic demo output.

### README presentation

The README adds a compact section near the top with:

- the `0% → 100%` result;
- a statement that this is a deterministic offline demo;
- copyable commands to initialize the fixture, run the experiment, compare variants, and generate a report;
- links to the checked-in result and the existing live Codex skill-ablation example.

## Data Flow

1. AgentAblate loads the example configuration and freezes the experiment matrix.
2. It creates a detached disposable Git worktree for each variant.
3. For `with-skill`, it installs the fingerprinted skill under `.agents/skills/`; for `baseline`, that directory is absent.
4. The command adapter runs the deterministic demo agent in the disposable worktree.
5. The evaluator checks `skill-demo-output.txt` after AgentAblate removes temporary skill inputs.
6. Trial results are written to SQLite.
7. `compare` aggregates the paired baseline and treatment trials and renders the success-rate delta.
8. `report` renders the full experiment summary.

## Error Handling

- A missing or malformed skill is rejected by existing configuration and skill validation.
- The `{python}` token is resolved before the command adapter doctor check and process launch; a literal non-placeholder executable still uses the existing availability check.
- A fixture that has not been initialized as a Git repository fails with the existing workspace error and is addressed by explicit setup commands.
- The demo agent treats unexpected or multiple demo skills as failure conditions instead of guessing.
- The checked-in representative result is regenerated and compared in tests so documentation cannot silently drift from behavior.

## Testing

Automated tests will:

- validate that all example files are packaged;
- verify that command-adapter `doctor()` and `run()` resolve exact `{python}` arguments to `sys.executable`;
- initialize a temporary copy of the fixture repository;
- run the complete example through the CLI or public experiment path;
- assert that `baseline` has `0/1` success;
- assert that `with-skill` has `1/1` success;
- assert that comparison output contains `+100.0 pp`;
- verify skill cleanup and that no source fixture state is mutated;
- keep the existing full test suite and lint checks passing.

## Acceptance Criteria

- A new user can reproduce the result offline with no API key.
- The demo completes quickly on supported Python versions.
- The baseline fails and the skill-enabled variant succeeds deterministically.
- The README clearly distinguishes the deterministic demo from real Codex evaluation.
- No production adapter receives demo-specific branching.
- The full test suite, Ruff, packaging checks, and example tests pass.
