# Codex Exec Adapter Design

Date: 2026-07-09
Status: approved for planning

## Goal

Add a first-party `codex-exec` adapter that runs real Codex coding trials locally,
including installations bundled with Codex Desktop, while preserving AgentAblate's
isolation, evidence, cancellation, and reproducibility guarantees. The same phase
also makes skill variants operational for Codex instead of merely fingerprinting
them.

## Scope

Phase 2 contains two implementation slices:

1. A reliable Codex CLI execution adapter with discovery, diagnostics, safe process
   execution, JSONL evidence capture, and runtime identity binding.
2. Repository-scoped skill installation inside each disposable trial worktree,
   followed by cleanup before evaluation.

MCP injection, Codex App Server, the Codex SDK, resumable Codex conversations,
token/cost aggregation, and provider comparison adapters are out of scope. A
`codex-exec` trial with a non-empty MCP variant must fail before starting Codex so
the experiment never silently ignores an intervention.

## User-facing configuration

The existing agent schema already accepts `adapter: codex-exec`. The minimal form
is:

```yaml
agents:
  - id: codex
    adapter: codex-exec
```

No credential is stored in experiment YAML. The adapter reuses the Codex CLI's
existing authentication. `doctor` reports the selected executable and version,
and explains how to authenticate when Codex is present but unusable.

Executable discovery checks `codex` on `PATH` first, followed by documented or
well-known application-bundle locations supported by tests. This includes the
Codex Desktop macOS bundle. Discovery failure is explicit and does not fall back
to the generic command adapter.

## Execution contract

`CodexExecAdapter` implements `AgentAdapter` and invokes an argument vector without
a shell. Its effective command is equivalent to:

```text
codex exec --json --color never --sandbox workspace-write --ephemeral
  --ignore-user-config -C <trial-worktree> <task-prompt>
```

The adapter never enables `danger-full-access` or bypasses approvals. It inherits
only AgentAblate's minimal environment (`PATH`, `HOME`, and `TMPDIR`), which allows
the selected Codex installation and its existing authentication to work without
copying credentials into evidence. Secrets are not added to the allowlist.

`--ignore-user-config` does not guarantee that Codex ignores user-level skills.
Therefore Phase 2 treats ambient user skills as part of the measured runtime rather
than claiming a clean-room baseline. Before matrix expansion, AgentAblate
fingerprints every regular file under the Codex user-skill locations discovered for
the current platform, including `$HOME/.agents/skills` and supported legacy
locations. That aggregate fingerprint is persisted in runtime identity and changes
the trial ID. Doctor warns when ambient skills are present, and reports the evidence
field that binds them. AgentAblate never reads credential files while computing this
fingerprint. Fully isolated Codex credential profiles are deferred because copying
or exposing authentication material would create a larger security surface.

Codex JSONL is retained as raw stdout for auditability. In this phase it is not
translated into every internal Codex event type; AgentAblate emits its existing
start and terminal lifecycle events. Process timeout, cancellation, process-tree
termination, event-sink failures, and partial stdout/stderr use the same semantics
as `CommandAdapter`. Shared process code should be extracted only where needed to
avoid divergent behavior.

A non-zero Codex exit remains an adapter failure and prevents the evaluator from
running.

## Skill variant isolation

For each selected skill directory, AgentAblate validates that a regular `SKILL.md`
exists and parses its frontmatter `name`. Names must satisfy the documented Agent
Skill name rules, and duplicate names within one variant are rejected. The install
directory is deterministic: a filesystem-safe normalized skill name followed by
the first 12 characters of its existing content fingerprint. AgentAblate copies the
directory into the disposable worktree at:

```text
.agents/skills/<normalized-name>-<fingerprint-prefix>/
```

This is Codex's repository-scoped skill discovery location. Installation happens
after the detached trial worktree is created and before Codex starts. The adapter
prompt explicitly invokes each validated frontmatter name using `$name` syntax so
the intended intervention is not left solely to implicit matching.

Skill trees may contain only directories and regular files. Symlinks, sockets,
devices, FIFOs, and other special files are rejected before fingerprinting or
copying. Fingerprints bind relative paths, file bytes, and executable permission
bits. Copying never follows links and revalidates the tree immediately before use,
preventing an input from escaping its declared directory or changing semantics
between identity generation and installation.

Before evaluation, AgentAblate removes only the injected paths and restores any
pre-existing paths byte-for-byte. Thus skill files cannot satisfy the task's test
command or appear as agent-generated changes. Installation and restoration are
per-worktree, so concurrent trials never write to shared `~/.codex` or to the
fixture repository.

Any installation or restoration failure fails the trial and is recorded. Cleanup
is attempted on success, failure, timeout, and cancellation.

## Reproducibility identity

Trial identity must include a Codex runtime fingerprint containing:

- resolved executable basename and SHA-256;
- normalized `codex --version` output;
- adapter identity schema version;
- effective safety and isolation flags.
- the aggregate fingerprint of ambient user-level skills.

Changing the executable, version, or effective adapter policy creates a new trial
ID, so `--resume` cannot reuse evidence from a different Codex runtime. Doctor and
execution must use the same discovery function. Matrix expansion freezes the
resolved absolute executable path, executable SHA-256, version, and ambient-skill
fingerprint into `TrialSpec`. Execution uses that frozen absolute path rather than
searching `PATH` again, and rechecks the executable hash and ambient-skill
fingerprint immediately before launch. A mismatch fails the trial before Codex
starts, closing the discovery-to-execution race. Existing variant skill
fingerprints remain part of trial identity.

## Integration points

- `adapters/codex_exec.py`: discovery, doctor, command construction, execution.
- `experiment.py`: doctor factory support for `codex-exec`.
- `runner.py`: default runtime factory and skill lifecycle integration.
- `identity.py` and `matrix.py`: adapter runtime fingerprint and trial-ID binding.
- `models.py`: only fields required to carry the frozen runtime identity; avoid
  general provider configuration in this phase.
- README and a real Codex example: authentication, Desktop support, cost/network
  warning, safety policy, and a baseline-versus-skill walkthrough.

The duplicate adapter registration in `experiment.py` and `runner.py` will remain
small and explicit in this phase. A broader registry refactor is not required for
one new adapter.

## Error handling and evidence

Errors distinguish missing executable, failed version probe, missing authentication,
invalid skill directory, unsupported MCP intervention, Codex non-zero exit, timeout,
cancellation, and cleanup failure. User-facing messages contain no credential values.
All persisted stdout, stderr, event data, and exception text continue through the
existing redaction layer.

## Verification

Tests must cover:

- PATH and Desktop-bundle discovery, missing executable, and version diagnostics;
- exact shell-free argv, minimal environment, and prompt handling;
- successful, non-zero, timed-out, and cancelled processes with partial evidence;
- event-sink failure behavior and process-tree cleanup parity;
- selection through both doctor and runner factories;
- runtime fingerprint changes producing different trial IDs;
- execution rejecting a changed or replaced frozen executable and changed ambient
  user skills;
- skill validation, installation, explicit prompt reference, restoration, and
  cleanup on every terminal path;
- invalid or duplicate skill names, symlinks, special files, permission changes,
  and deterministic install names;
- preservation of pre-existing `.agents/skills` content and concurrent isolation;
- fail-fast behavior for non-empty MCP variants;
- a fake Codex executable integration test that avoids network usage and billing.

The normal Ruff, full pytest/coverage suite, package build, and cross-version CI
matrix remain required. A live billed Codex run is a documented manual smoke test,
not a required CI check.

## Delivery

Implementation follows test-driven development and is split into reviewable
commits: execution/runtime identity first, then skill lifecycle and documentation.
Each commit is independently reviewed and pushed to `feat/codex-exec-adapter` so it
can be inspected from another machine. The final result is submitted as a draft PR.
