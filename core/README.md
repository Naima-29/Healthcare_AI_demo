# kcd_core: the base every seat runs on

Owner: Person 1 (Core). Scope: contracts, the seat runtime and its policy enforcement point (PEP), signed A2A
messages, the bus, the Dispatcher with its escalation ladder, and the manifest registry.

```bash
uv sync
uv run pytest -q                              # 124 tests, under 1 s, no Docker needed
uv run python scripts/export_schemas.py       # regenerate contracts/schemas after a contract change
```

## Layout

| Path | What |
|---|---|
| `core/kcd_core/contracts/` | C1 `Manifest`, C2 `Envelope` + the 10 payloads, C3 `PolicyCatalog`, C5 `Release` (Pydantic, strict) |
| `contracts/schemas/` | JSON Schemas generated from the models, for the web app and other teams. CI fails if stale |
| `contracts/fixtures/` | Golden fixtures: valid and invalid payloads for each message type |
| `contracts/policy_catalog.json` | Refusals, tools, surfaces, gate roles. Clinically owned |
| `seats/manifests/` | Seed manifests for the 9 seats. Each owner edits their own |
| `core/kcd_core/pep.py` | Deny-by-default checks on tools, peers, memory layers and release-gated content |
| `core/kcd_core/runtime.py` | `Seat` base class, `SeatContext` (the only way a handler touches the world), `SeatRuntime` |
| `core/kcd_core/dispatcher.py` | Verify, validate, route from manifests, dedupe, open escalations and gates |
| `core/kcd_core/escalation.py` | On-call paging ladder with injectable clock, rota and pager |
| `core/kcd_core/bus.py` | `Bus` port + `InMemoryBus` (retry with backoff, dead-letter) |
| `core/kcd_core/signing.py` | Ed25519 over RFC 8785 canonical JSON, for envelopes and clinician releases |
| `core/kcd_core/audit.py` | Hash-chained audit events |
| `infra/docker-compose.yml` | Dev edge stack (Postgres+pgvector, NATS JetStream, MinIO, Ollama, HAPI FHIR, Orthanc) |

## Writing a seat (Persons 2 to 5)

```python
from kcd_core.contracts import MessageType
from kcd_core.contracts.messages import MedicationReport, TherapyConflict
from kcd_core.runtime import Seat

class RenalGuardian(Seat):
    seat_id = "renal_guardian"

    async def handle(self, env, payload: MedicationReport, ctx):
        labs = await ctx.tool("fhir.read_observations", patient=env.patient_id, code="egfr")  # PEP-checked
        ...
        await ctx.send(MessageType.THERAPY_CONFLICT, "safety_verifier", TherapyConflict(...))  # signed, routed
```

The rules the runtime enforces, so a seat cannot get them wrong:

- A handler only receives types listed in its manifest's `consumes`, already signature-checked and parsed.
- `ctx.tool` is blocked unless the tool is in `tools`, not refused, and its surface is granted. A refusal
  wins even when the tool is also listed. `ctx.visible_tools()` is the list you give the model.
- `ctx.send` is blocked unless the type is in `emits` and the receiver is in `a2a_peers`. Messages always go
  through the Dispatcher, which checks the route again against both manifests.
- `ctx.guard_content("diagnosis", artifact_hash, release)` blocks release-gated content unless there is a
  valid clinician release for that exact artifact hash and content class.
- A `PolicyViolation` or a timeout calls your `fallback()` and is not retried. Any other exception is retried
  by the bus, then dead-lettered.
- A manifest activated in the registry applies from the next message on (hot reload), and `rollback()` undoes it.

## Decisions and assumptions (change them here, not in code)

1. **Own thin runtime, not Loop Nexus/Orchestra.** Everything external sits behind a port (`Bus`, `KeyRing`,
   `AuditSink`, `EscalationStore`, `OnCallRota`, `Pager`, `GatePort`, `ToolRegistry`), so a platform can be
   swapped in later without changing seat code.
2. **Pydantic is the source of truth** for contracts and JSON Schema is generated from it. The plan said the
   reverse; this way there is one definition in the language most of the system is written in, and CI
   keeps the schemas in sync.
3. **The 10 types and their fields were rebuilt from the engineering plan**, because the demo's
   `contracts/*.mjs` is not in this repo. Diff them against the demo before the week-1 freeze.
4. **`consumes` / `emits` added to the manifest (C1).** The Dispatcher derives routing from them; there is no
   separate routing config.
5. **All clinical outputs pass through the Safety Verifier.** The seed manifests force
   `renal_guardian / imaging_reader / genomics_interpreter → safety_verifier → case_briefer`. A direct route is
   rejected by the Dispatcher (tested).
6. **The Dispatcher does not re-sign.** The sender's signature travels end to end, so the receiver knows
   who really wrote the message.
7. **Delivery is at-least-once with dedupe** on `idempotency_key`, marked only after delivery succeeded. A
   redelivered red flag never pages twice.

## Next (Core)

- `NatsJetStreamBus` + transactional outbox in Postgres; Postgres stores for audit, idempotency, escalations
- MCP connector template + `oncall` and `fhir` reference servers
- Chief of Staff: draft, safety floor (refusals unioned in, never dropped), validate, diff, approve, hot-reload
- Escalation `tick()` worker using `SELECT … FOR UPDATE SKIP LOCKED`; real pager behind a feature flag
