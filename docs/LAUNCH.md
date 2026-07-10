# AgentAblate launch kit

This file keeps the first promotion wave concrete and repeatable. The goal is not
to spam links; it is to explain the project clearly to people who already care
about coding agents, skills, MCP, and reproducible evaluation.

## Positioning

AgentAblate is a local experiment harness for coding-agent changes. It helps answer:

> Did this skill, prompt, MCP server, or tool policy actually improve the agent?

Use the phrasing below consistently:

- **Short tagline**: Local ablation testing for coding-agent skills, prompts, MCP
  servers, and tool policies.
- **One-liner**: AgentAblate compares baseline-versus-extension coding-agent runs
  in disposable Git worktrees and preserves the raw evidence.
- **Who it is for**: coding-agent builders, prompt/skill authors, MCP server
  developers, and empirical software-engineering researchers.
- **Why now**: agent tooling changes fast, but most teams still judge prompt and
  skill changes by anecdotes.

## First-wave channels

Start with high-signal communities where the repo is directly relevant:

1. GitHub profile / pinned repository
2. X / Twitter
3. LinkedIn
4. Reddit: `r/LocalLLaMA`, `r/MachineLearning`, `r/programming` only when the post
   is framed as an engineering/research artifact rather than an ad
5. Hacker News: “Show HN” once the README, example, and CI are stable
6. Discord/Slack communities for Codex, MCP, agent engineering, and programming
   tools
7. Personal website or blog post when there is a concrete mini case study

Avoid posting the same message everywhere on the same day. A useful cadence is:

- Day 0: GitHub README polish, topics, pinned repo
- Day 1: X/LinkedIn announcement
- Day 3: short technical thread with one example result
- Day 5: community post asking for task-suite/adapters feedback
- Day 7+: Show HN or longer blog post after at least one external user question

## GitHub repository checklist

- Add repository topics: `coding-agents`, `codex`, `mcp`, `agent-evaluation`,
  `ablation-testing`, `llm-agents`, `software-engineering`, `benchmarking`.
- Pin the repository on the GitHub profile.
- Keep the README first screen focused on the core question and quick proof.
- Make issues easy to open:
  - “Add an adapter”
  - “Contribute a task suite”
  - “Report reproducibility gap”
- Label beginner-friendly issues only when they are genuinely scoped.

## Copy-paste announcement

### Short post

I built AgentAblate, a local ablation harness for coding-agent changes.

It asks a simple question: did this skill, prompt, MCP server, or tool policy
actually improve the agent?

It runs baseline-vs-extension trials in disposable Git worktrees, fingerprints the
inputs, stores raw evidence in SQLite/JSONL, and now includes a first-party Codex
CLI adapter for skill ablations.

Repo: https://github.com/ShiHuiwen-creat/AgentAblate

### Technical thread

1. Coding-agent changes are easy to make and hard to evaluate. A new skill or MCP
   server can feel better after one lucky run.
2. AgentAblate treats those changes as experimental variables: baseline vs.
   extension, same task revision, same evaluator.
3. Runs happen locally in disposable Git worktrees. Trial identity binds config,
   task revision, evaluator identity, extension fingerprints, and Codex runtime
   identity.
4. The new `codex-exec` adapter invokes Codex without a shell, with a minimal
   environment and workspace-write sandbox. Variant skills are installed only
   during agent execution and removed before evaluation.
5. It is early, but already useful for building reproducible task suites and
   turning “this prompt feels better” into inspectable evidence.
6. Feedback wanted, especially on adapters, task-suite design, and metrics.
   https://github.com/ShiHuiwen-creat/AgentAblate

### Hacker News / Reddit style

I’m building AgentAblate, a local ablation-testing harness for coding agents.

The motivation is that skills, prompts, MCP servers, and tool policies often
change together, so it becomes hard to tell which intervention helped. AgentAblate
runs baseline-versus-extension trials in disposable Git worktrees, fingerprints the
inputs, stores raw evidence, and generates reports.

The current version supports deterministic fake runs, custom command adapters, and
a first-party `codex-exec` adapter for Codex CLI skill ablations. It is not a
leaderboard; it is meant to be a small, auditable research/engineering artifact.

I’d especially like feedback on task-suite design, adapter interfaces, and what
metrics should be added next.

Repo: https://github.com/ShiHuiwen-creat/AgentAblate

## Follow-up content ideas

- “How to test whether a Codex skill helps”
- “Why agent benchmarks need raw evidence, not just pass rates”
- “A minimal reproducible coding-agent task fixture”
- “Skill ablation vs. prompt ablation vs. MCP ablation”
- “What AgentAblate records in a trial ID”

## Star conversion principles

- Lead with the problem, not the implementation.
- Show one concrete command or report before explaining internals.
- Ask for specific feedback, not generic attention.
- Keep claims modest: local, reproducible, early, inspectable.
- Treat every question as material for docs and examples.
