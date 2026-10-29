# Healthcare_AI_demo

Kidney Care Desk. See [ENGINEERING_PLAN.md](ENGINEERING_PLAN.md) for the architecture.

## Core (Person 1)

Contracts, seat runtime, policy enforcement, signed A2A messages, Dispatcher and escalation ladder.
Details and seat-author guide: [core/README.md](core/README.md).

```bash
uv sync
uv run pytest -q
```
