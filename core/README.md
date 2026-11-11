# kcd_core: the base every seat runs on

Owner: Person 1 (Core). Scope: contracts, the seat runtime and its policy enforcement point (PEP), signed A2A
messages, the bus, the Dispatcher with its escalation ladder, the manifest registry, and the Chief of Staff's
safety floor. The vocabulary (refusals, gates, tools, manifest fields, message payloads) follows the v2 demo in
`demo/kidney-care-desk`.

```bash
uv sync
uv run pytest -q                              # 116 tests, under 1 s, no Docker needed
uv run python scripts/export_schemas.py       # regenerate contracts/schemas after a contract change
uv run python scripts/import_demo_manifests.py  # re-import seat manifests after the demo seeds change
```

## Layout

| Path | What |
|---|---|
| `core/kcd_core/contracts/` | C1 `Manifest`, C2 `Envelope` + the 10 payloads, C3 `PolicyCatalog`, C5 `Release` (Pydantic, strict) |
| `contracts/schemas/` | JSON Schemas generated from the models, for the web app and other teams. CI fails if stale |
| `contracts/fixtures/` | Golden fixtures: valid payloads from the demo's golden path, plus invalid ones |
| `contracts/policy_catalog.json` | The demo's 20 refusals, 8 gates and MCP surfaces, plus Core's `tool_actions`. Clinically owned |
| `seats/manifests/` | The demo's 8 seat manifests (imported) + `chief_of_staff`. Generated: do not hand-edit the 8 |
| `seats/jobs/` | The demo's job descriptions, one per seat |
| `scripts/import_demo_manifests.py` | Demo manifests → `seats/manifests`, adding routes (`a2a_peers`, `consumes`, `emits`) and reviewed overrides |
| `core/kcd_core/chief_of_staff/` | The demo compiler's rule pass in Python: `amend`, `compile_new`, `enforce_floor` |
| `core/kcd_core/pep.py` | Deny-by-default checks on tools, peers, memory layers and release-gated content |
| `core/kcd_core/runtime.py` | `Seat` base class, `SeatContext` (the only way a handler touches the world), `SeatRuntime` |
| `core/kcd_core/dispatcher.py` | Verify, validate, route from manifests, dedupe, open escalations and gates |
| `core/kcd_core/escalation.py` | On-call paging ladder with injectable clock, rota and pager |
| `core/kcd_core/bus.py` | `Bus` port + `InMemoryBus` (retry with backoff, dead-letter) |
| `core/kcd_core/signing.py` | Ed25519 over RFC 8785 canonical JSON, for envelopes and clinician releases |
| `core/kcd_core/audit.py` | Hash-chained audit events |
| `core/kcd_core/adapters/` | `NatsJetStreamBus`, Postgres stores (audit, idempotency, escalations, rota, gate queue), transactional outbox + relay |
| `core/kcd_core/service.py`, `settings.py`, `workers.py`, `pagers.py` | The deployable process, env config with fail-fast checks, background loops, log/webhook pagers |
| `core/kcd_core/control_plane.py`, `api.py` | Propose / approve / rollback manifests, store, reloader; HTTP + OIDC auth (fail closed) |
| `core/kcd_core/chief_of_staff/diff.py`, `llm.py` | Field-level manifest diff; LLM draft step constrained by deterministic code |
| `core/kcd_core/connectors/` | MCP connector template, `oncall` and `fhir` reference servers, bridge into `ToolRegistry` |
| `core/kcd_core/migrate.py`, `infra/migrations/` | Checksummed SQL migrations (001 draft, 002 hardening) |
| `infra/docker-compose.yml` | Dev edge stack (Postgres+pgvector, NATS JetStream, MinIO, Ollama, HAPI FHIR, Orthanc) |

## Writing a seat (Persons 2 to 5)

```python
from kcd_core.contracts import MessageType
from kcd_core.contracts.messages import MedicationReport, TherapyConflict
from kcd_core.runtime import Seat

class RenalGuardian(Seat):
    seat_id = "renal_guardian"

    async def handle(self, env, payload: MedicationReport, ctx):
        labs = await ctx.tool("fhir.get_observations", patient=env.patient_id, code="egfr")  # PEP-checked
        ...
        await ctx.send(MessageType.THERAPY_CONFLICT, "safety_verifier", TherapyConflict(...))  # signed, routed
```

The rules the runtime enforces, so a seat cannot get them wrong:

- A handler only receives types listed in its manifest's `consumes`, already signature-checked and parsed.
- `ctx.tool` is blocked unless the tool is in `tools` and its surface (MCP server) is granted, and is always
  blocked if the tool performs a refused action (`tool_actions` in the catalogue), even when it is listed.
  `ctx.visible_tools()` is the list you give the model.
- `ctx.act("case.decide")` declares an action that is not a tool call; it raises if a refusal blocks it.
- `ctx.send` is blocked unless the type is in `emits` and the receiver is in `a2a_peers`. Messages always go
  through the Dispatcher, which checks the route again against both manifests.
- `ctx.guard_content("imaging_result", artifact_hash, release)` blocks release-gated content unless there is a
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
3. **Contracts follow the v2 demo.** Same 10 message types, the demo's field names (so its golden-path
   messages validate here), its catalogue and manifest fields. Core keeps on top: the envelope's
   `episode_id` / `causation_id` / `idempotency_key` / `manifest_version`, `artifact_hash` on case packs and
   decisions (the clinician's release binds to it), and strict payloads: an undeclared field is rejected, which
   closes the demo's `verification.passed` bypass of the Safety Verifier.
4. **`consumes` / `emits` added to the manifest (C1).** The Dispatcher derives routing from them; there is no
   separate routing config.
5. **All clinical outputs pass through the Safety Verifier.** Routes come from the demo Dispatcher's switch,
   with one change: genomic context also goes through the verifier (the demo sends it straight to the Case
   Briefer). A direct route is rejected by the Dispatcher (tested).
   **Peers mean something different from the demo:** there every seat lists only `dispatcher`; here
   `a2a_peers` are the seats a seat may address, and the Dispatcher is the transport between them.
6. **The Dispatcher does not re-sign.** The sender's signature travels end to end, so the receiver knows
   who really wrote the message.
7. **Delivery is at-least-once with dedupe** on `idempotency_key` (sender, receiver, type, episode, payload),
   marked only after delivery succeeded. A redelivered red flag never pages twice. Gate opening must be
   idempotent on (case_id, version) because a retry can repeat it.
8. **Manifest versions are integers** (`companion@v2`), as in the demo. Contract versions stay semver.
9. **Memory layers follow the demo:** L0/L1 licensed knowledge (read-only for seats), L2 hospital, L3 patient.
10. **One reviewed override of the demo seeds:** the Companion refuses `disclose_imaging_before_review` from
    day one (the demo adds it live on stage). Overrides live in `scripts/import_demo_manifests.py`.

## Running Core (infrastructure, deployment)

Everything below is exercised by the test suite against a real Postgres 16 and NATS JetStream.

```bash
docker compose -f infra/docker-compose.yml up -d postgres nats        # infra for tests and local runs
uv sync --all-extras && uv run pytest                                  # adapter + end-to-end tests run for real
python scripts/gen_keys.py                                             # dev keypairs for every seat -> ./keys (git-ignored)
docker compose -f infra/docker-compose.yml up -d --build core          # migrate (one-shot) -> core service on :8000
curl localhost:8000/readyz                                             # {"database":true,"bus":true,...}
```

`python -m kcd_core.service` runs the dispatcher, outbox relay, escalation worker, manifest reloader and the HTTP
server (`/healthz` liveness, `/readyz` readiness, `/v1/manifests/*` Director API). `python -m kcd_core.migrate`
applies `infra/migrations` (checksummed, advisory-locked, safe to run from every replica).

| Variable | Default | Notes |
|---|---|---|
| `KCD_ENV` | `dev` | `staging`/`prod` refuse to start with the log pager, a dev token, the dev DB password, or (prod) < 3 JetStream replicas |
| `KCD_DATABASE_URL`, `KCD_NATS_URL` | local dev | |
| `KCD_KEYS_DIR` | `keys` | `<seat>.pub` for every seat (dispatcher); `<seat>.key` (mode 600) only where that seat runs |
| `KCD_PAGER` | `log` | `webhook` needs `KCD_PAGER_URL` + `KCD_PAGER_SECRET` (HMAC-SHA256 in `X-KCD-Signature`) |
| `KCD_OIDC_ISSUER/AUDIENCE/JWKS_URL` | unset | unset = the control-plane API answers 401 to everything (fail closed) |
| `KCD_LLM_BASE_URL`, `KCD_LLM_MODEL` | unset | OpenAI-compatible endpoint for the Chief of Staff draft step; unset = rule-based drafts |
| `KCD_MIGRATE_ON_START` | `false` | convenient for dev; production runs the `migrate` job first |

### Delivery guarantees (what to rely on, what not to)

- **At-least-once everywhere, duplicates absorbed by idempotency keys.** A duplicate page is acceptable, a missing page is not.
- **Dispatcher**: the idempotency mark and the outbound message commit in one Postgres transaction (transactional outbox); a relay publishes with a JetStream message id (`<envelope id>@<subject>`; the subject is part of it because the same envelope legitimately crosses two hops).
- **Not ordered.** A failing outbox row backs off alone; later rows go past it, because head-of-line blocking a red flag is worse than reordering. Nothing in the protocol depends on order.
- **Dead letters are durable** (`KCD_DLQ` stream, `dlq.<subject>`, error and attempts in headers). If the dead-letter write itself fails, the message is nak'd, never dropped. Alert on stream depth and on `/readyz` `outbox_dead`.
- **Escalations never go silent**: a failed page, a rota outage, nobody on call, or a crashed worker all climb the ladder; workers claim rows with `FOR UPDATE SKIP LOCKED` + a lease; terminal states are compare-and-set so a late tick cannot un-acknowledge.
- **Audit is append-only in the database** (triggers reject UPDATE/DELETE/TRUNCATE) and hash-chained (`verify_chain()` still catches an owner who disables the triggers).
- **Sync ports on purpose.** The Core ports are synchronous so seat authors keep a simple contract; adapters use short statements with connect/statement timeouts, and the background loops use threads. Moving the ports to async later is a mechanical change.

### Control plane (Chief of Staff)

Propose (`POST /v1/manifests/drafts`) -> the response carries a field-level diff with `[WIDENS]` / `[LOOSENS]` flags ->
Director approves (`.../approve`) -> catalogue + topology check -> stored -> every process converges via
`ManifestReloader` (no restart). A draft made against an older live version is refused as stale. The LLM step can
only shape tools, surfaces, refusals, gates and wording; routes, memory, cloud fallback and the seat's identity come
from trusted code, and `enforce_floor` means it can add restrictions but never remove one.

### Connectors (`kcd_core/connectors`)

`base.py` is the template (steps in its docstring). `oncall.py` and `fhir.py` are reference servers whose tool names
and call shapes match the catalogue and the seats (`patient=`, `code="eGFR"`). `bridge.bind_session` plugs any MCP
session into a seat's `ToolRegistry`; the PEP still decides every call before it reaches a connector.
Run one: `KCD_FHIR_URL=http://localhost:8080/fhir python -m kcd_core.connectors.fhir`.

## Known gaps (deliberately not done here)

- Ed25519 seat keys are read from files; wiring them to Vault/KMS is a deployment task.
- The gate service is a durable queue (`gate_item`) until the Board owner's service replaces `PostgresGateQueue`.
- No metrics endpoint yet; `/readyz` exposes outbox depth and dead rows as the minimum.
- Nothing here has run on a multi-node NATS cluster (replicas > 1) or behind a real Keycloak/paging gateway.
- The Dockerfile and CI workflow were written and simulated (clean install, migrate, boot, SIGTERM) but not built with Docker in this environment.
