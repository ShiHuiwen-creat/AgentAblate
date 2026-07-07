# AgentAblate

AgentAblate makes baseline-versus-extension experiments for coding agents local,
repeatable, and easy to inspect.

> Phase 1 preview: the fake and custom-command adapters are implemented today;
> first-party coding-agent adapters are on the roadmap.

## See the result

Open the checked-in [sample Markdown report](docs/sample-report.md) or
[sample HTML report](docs/sample-report.html). They show the same deterministic
fake-adapter run used in the quickstart below.

| Variant | Success |
|---|---:|
| `baseline` | 1/1 |
| `with-skill` | 1/1 |

## Five-minute quickstart

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

Without uv, create a standard environment with `python -m venv .venv`, activate it,
and run `python -m pip install -e '.[dev]'`. On Windows, invoke
`..\\..\\.venv\\Scripts\\agentablate` in the final commands. The example's test
uses the `python` launcher from `PATH`, so keep the activated environment first.
The fixture's nested `.git` directory is local setup and must not be committed.

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

## Metrics and reproducibility

Phase 1 reports success count, success rate, mean duration, failure categories, and
paired success deltas against `baseline`. Every trial resolves the fixture revision
to an immutable Git commit OID, fingerprints skill and MCP content, and receives a
deterministic ID from its inputs. Runs happen in detached disposable Git worktrees.
Structured trial state is stored in SQLite and event streams in append-only JSONL,
so interrupted runs can be resumed and raw evidence remains inspectable.

AgentAblate does not yet calculate confidence intervals or claim statistical
significance. Use enough tasks and repetitions for the question you are studying,
and treat the raw records as the source of truth.

## Adapter roadmap

- Available in Phase 1: deterministic `fake` and bring-your-own `command` adapters.
- Planned: Codex SDK/CLI/App Server, Claude Code, and Gemini CLI adapters.
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
