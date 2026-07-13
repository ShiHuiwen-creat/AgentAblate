# AgentAblate

[![CI](https://github.com/ShiHuiwen-creat/AgentAblate/actions/workflows/ci.yml/badge.svg)](https://github.com/ShiHuiwen-creat/AgentAblate/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)

AgentAblate is a local experiment harness for answering one stubborn coding-agent
question:

> Did this skill, prompt, MCP server, or tool policy actually improve the agent?

It runs baseline-versus-extension trials in disposable Git worktrees, records the
raw evidence, and produces reports you can inspect instead of trusting vibes.

> Phase 1 preview: the fake, custom-command, and first-party `codex-exec`
> adapters are implemented today. Other coding-agent adapters remain on the
> roadmap.

## Why people use it

- **Ablate agent changes**: compare baseline vs. skill/prompt/tool variants while
  holding the task and evaluator fixed.
- **Keep runs reproducible**: trial IDs bind config, task revision, evaluator
  identity, extension fingerprints, and Codex runtime identity.
- **Stay local-first**: fixtures run in isolated Git worktrees and results live in
  SQLite plus JSONL event logs.
- **Test Codex skills safely**: `codex-exec` invokes the local Codex CLI without a
  shell, with a minimal environment and workspace-write sandbox.
- **Build research artifacts**: reports and raw records are designed for empirical
  coding-agent work, not just demos.

## See the result

Open the checked-in [sample Markdown report](docs/sample-report.md) or
[sample HTML report](docs/sample-report.html). These representative files are
golden-tested against the current renderers and use the fake-adapter experiment
from the quickstart below; measured duration varies by machine.

| Variant | Success |
|---|---:|
| `baseline` | 1/1 |
| `with-skill` | 1/1 |

## Five-minute local quickstart

Clone the repository, install it, prepare the example fixture, and run the ablation:

```bash
git clone https://github.com/ShiHuiwen-creat/AgentAblate.git
cd AgentAblate
uv venv
uv pip install -e '.[dev]'
git -C examples/fake-ablation/fixture init
git -C examples/fake-ablation/fixture config user.name AgentAblate
git -C examples/fake-ablation/fixture config user.email agentablate@example.invalid
git -C examples/fake-ablation/fixture add README.md
git -C examples/fake-ablation/fixture commit -m "Initial fixture"
cd examples/fake-ablation
../../.venv/bin/agentablate doctor
../../.venv/bin/agentablate run
../../.venv/bin/agentablate compare
../../.venv/bin/agentablate report --format markdown
../../.venv/bin/agentablate report --format html
```

The report commands write `agentablate-report.md` and `agentablate-report.html`
in `examples/fake-ablation`. Pass `--output <path>` to choose another destination.

Without uv, create a standard environment with `python -m venv .venv`, activate it,
and run `python -m pip install -e '.[dev]'`. On Windows, invoke
`..\\..\\.venv\\Scripts\\agentablate` in the final commands. In task commands, an
argument exactly equal to `{python}` resolves to the interpreter running
AgentAblate; no activation or platform-specific Python path is needed. Text that
merely contains `{python}` is left unchanged. The fixture's nested `.git` directory
is local setup and must not be committed.

## Baseline versus skill

An experiment is an explicit matrix. The included example changes only the skill:

```yaml
agents:
  - id: fake
    adapter: fake
variants:
  - id: baseline
  - id: with-skill
    skills:
      - ./skill
tasks:
  - ./task.yaml
```

The fake adapter is deterministic plumbing for validating experiment setup. The
command adapter runs a user-supplied executable. Neither claims to measure a real
coding agent; use them to develop tasks and integrations without API cost.

## Codex CLI skill ablations

The `codex-exec` adapter runs the local Codex CLI with a frozen executable,
version, policy, and ambient-skill fingerprint in each trial ID. Install the
Codex CLI or Codex Desktop CLI, run `codex login`, and check discovery with:

```bash
agentablate doctor examples/codex-skill-ablation/agentablate.yaml
```

On macOS, AgentAblate first uses `codex` on `PATH`, then the Codex Desktop CLI at
`/Applications/Codex.app/Contents/Resources/codex`. On other platforms it uses
`PATH` discovery only.

Live Codex runs consume your Codex quota, may use the network, and are intentionally
manual. For the packaged example:

```bash
git -C examples/codex-skill-ablation/fixture init
git -C examples/codex-skill-ablation/fixture config user.name AgentAblate
git -C examples/codex-skill-ablation/fixture config user.email agentablate@example.invalid
git -C examples/codex-skill-ablation/fixture add README.md
git -C examples/codex-skill-ablation/fixture commit -m "Initial fixture"
agentablate doctor examples/codex-skill-ablation/agentablate.yaml
agentablate run examples/codex-skill-ablation/agentablate.yaml --variant with-skill
agentablate compare examples/codex-skill-ablation/.agentablate/results.sqlite3
agentablate report examples/codex-skill-ablation/.agentablate/results.sqlite3 --format markdown
```

Codex is always invoked without a shell and with `--json --color never --sandbox
workspace-write --ephemeral --ignore-user-config`. AgentAblate passes only a
minimal allowlisted environment, never uses `danger-full-access`, and fails before
launch if a `codex-exec` trial declares MCP inputs. Ambient user skills are not
copied into trial worktrees, but their fingerprint is captured as evidence because
they can affect the local Codex runtime. Variant skills are installed only around
agent execution and removed before evaluation.

## When AgentAblate is a good fit

Use it when you want to compare agent changes across tasks and keep the evidence:

- Does adding a repo-specific Codex skill improve success rate?
- Does an MCP server help, hurt, or just add variance?
- Did a prompt policy change make failures less frequent?
- Can a coding-agent benchmark be reproduced from a clean checkout?

It is not a leaderboard, hosted benchmark, or substitute for careful task design.
Start with small task suites, inspect failures, then scale repetitions once the
setup is trustworthy.

## Metrics and reproducibility

Phase 1 reports success count, success rate, mean duration, failure categories, and
paired success deltas against `baseline`. Every trial resolves the fixture revision
to an immutable Git commit OID, fingerprints skill and MCP content, and receives a
deterministic ID from its inputs. Runs happen in detached disposable Git worktrees.
Structured trial state is stored in SQLite and event streams in append-only JSONL,
so interrupted runs can be resumed and raw evidence remains inspectable.

The `{python}` placeholder is portable across operating systems, but portability
does not erase environment differences. Trial identity captures the AgentAblate and
evaluator schema versions, Python implementation and full version, platform
release/version, a normalized installed Python-distribution snapshot hash, and the
resolved evaluator executable's portable name and content hash. For Python `-m`
evaluators it also records the corresponding distribution version when discoverable.
Changes to this captured identity produce a new trial ID, preventing `--resume` from
reusing mismatched evidence.

External services and unobserved environment state are not captured automatically.
Represent those changes with a new configuration or repetition when they may affect
results.

AgentAblate does not yet calculate confidence intervals or claim statistical
significance. Use enough tasks and repetitions for the question you are studying,
and treat the raw records as the source of truth.

## Adapter roadmap

- Available in Phase 1: deterministic `fake`, bring-your-own `command`, and
  first-party `codex-exec` adapters.
- Planned: Codex SDK/App Server, Claude Code, and Gemini CLI adapters.
- Later: richer token/cost metrics and statistical summaries built on raw events.

Roadmap entries are intentions, not currently supported integrations.

## Research motivation

Skills, prompts, MCP servers, and tool policies often change together, making it
hard to tell which intervention helped. AgentAblate treats an agent configuration as
an experimental variable while holding the task revision and evaluator fixed. The
goal is a small, auditable bridge between day-to-day agent engineering and empirical
software-engineering research: reproducible artifacts first, interpretation second.

## Contributing

Issues, task suites, adapters, and careful experimental critiques are welcome. Read
[CONTRIBUTING.md](CONTRIBUTING.md) for the TDD workflow, adapter contract, credential
rules, and pull-request checklist. AgentAblate is licensed under Apache-2.0.

If this project helps your agent workflow or research, a star makes it easier for
other coding-agent builders to find.
