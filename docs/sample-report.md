# AgentAblate report

This checked-in report illustrates the deterministic fake-adapter example. Runtime
durations are normalized here because they vary slightly by machine.

- Config hash: fec504b5f3136666324b7a36c7b11bd1c2d29b9c105a783fc3270ed8cb2ad899
- Adapter implementation/version: fake / 0.1.0.dev0
- Repetitions: 1

| Agent | Variant | Task | Success | Success rate | Mean duration (s) | Failure reason |
|---|---|---|---:|---:|---:|---|
| fake | baseline | create-output | 1/1 | 100.0% | 0.000 | none |
| fake | with-skill | create-output | 1/1 | 100.0% | 0.000 | none |

The equal result is intentional: the fake adapter validates the ablation pipeline,
not the skill's effect on a real model.
