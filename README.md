# Kidney Care Desk

A care desk of AI "seats" that watches patients with kidney disease between visits. Each seat is an agent with one
job (talk to the patient, read CT scans, check drugs against kidney function, brief the doctor, and so on). Seats
talk only through typed, signed messages, and **a clinician signs before anything clinical reaches the patient**.
Safety rules are enforced in code, never only in a prompt.

- Architecture and roadmap: [ENGINEERING_PLAN.md](ENGINEERING_PLAN.md)
- Core runtime internals and the seat-author guide: [core/README.md](core/README.md)
- Andrea's v2 demo (read-only reference, the source of our vocabulary): [demo/README.md](demo/README.md)

> Research prototype. Synthetic or sandbox data only, until clinical validation and regulatory review are done.

## Who owns what

| Person | Owns |
|---|---|
| 1. Core (Harith) | Contracts, seat runtime + policy enforcement, signed messages, Dispatcher + escalation, Chief of Staff, MCP connector template |
| 2. Patient side (Meet) | Companion: Telegram, interview, emotion signals, red flags, safety script, wearables |
| 3. Evidence (Chrislin, Naima) | Imaging Reader, Genomics Interpreter, Cortex memory, Archivist |
| 4. Clinical reasoning (Diana) | Renal Guardian, Safety Verifier, Knowledge Model |
| 5. Doctor and memory (Naima) | Case Briefer, Clinical Board, Memory panel, CT model training + heat map |

Everyone builds on Core: you write a seat against the contracts in `core/kcd_core/contracts`, and the runtime
handles signing, routing, policy checks and audit for you.

## Quickstart

You need Python 3.11 or newer (3.12 to 3.14 tested). Pick one of the two setups.

**With uv** (recommended, uses the lock file):

```bash
uv sync
uv run pytest -q
```

**With plain pip, Windows (PowerShell):**

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip      # pip 25.1+ is needed for --group
python -m pip install -e .
python -m pip install --group dev
python -m pytest -q
```

If PowerShell refuses to run `Activate.ps1`, run `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` once.

**With plain pip, macOS / Linux:**

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .
python -m pip install --group dev
python -m pytest -q
```

You should see `116 passed` in about a second. No Docker, database or model server is needed for the tests;
everything runs on in-memory adapters.

### Checks CI runs on every push

```bash
ruff check .
mypy
python scripts/export_schemas.py --check   # JSON Schemas match the Python contracts
pytest -q
```

(Prefix with `uv run` if you use uv.)

### Dev infrastructure (optional for now)

```bash
docker compose -f infra/docker-compose.yml up -d
```

Starts Postgres + pgvector, NATS JetStream, MinIO, Ollama, HAPI FHIR and Orthanc. Nothing in Core uses it yet;
it is the target for the Postgres and NATS adapters and for the MCP servers.

## Project layout

```
contracts/
  policy_catalog.json     the demo's 20 refusals, 8 gates, MCP surfaces + Core's tool_actions (clinically owned)
  schemas/                JSON Schemas generated from the Python contracts (do not edit by hand)
  fixtures/               valid payloads from the demo's golden path + invalid ones, per message type
core/kcd_core/            the Core package: contracts, runtime, policy enforcement, bus, dispatcher
core/kcd_core/chief_of_staff/  the demo compiler's rule pass in Python (amend, compile_new, enforce_floor)
seats/manifests/          one manifest per seat; the 8 demo seats are generated, chief_of_staff is hand-written
seats/jobs/               the demo's plain-language job description for each seat
scripts/export_schemas.py        regenerates contracts/schemas
scripts/import_demo_manifests.py demo seeds -> seats/manifests (adds routes and reviewed overrides)
demo/kidney-care-desk/    Andrea's v2 demo (Node), kept as received; do not edit, CI and ruff ignore it
tests/                    contract, policy, signing, dispatcher, escalation, runtime and Chief of Staff tests
infra/docker-compose.yml  dev edge stack
```

## Building a seat (Persons 2 to 5)

1. **Change your seat's manifest through the importer**, not by hand: routes (`a2a_peers`, `consumes`, `emits`)
   are in `ROUTES` and reviewed changes in `OVERRIDES` in `scripts/import_demo_manifests.py`; then run
   `python scripts/import_demo_manifests.py`. The Dispatcher routes from these fields, so there is no separate
   routing config. Run the tests: a manifest that names an unknown tool, refusal, gate or peer fails at load.
2. **Subclass `Seat`** and implement `handle()`. Use `ctx.tool(...)` for tools and `ctx.send(...)` for messages;
   both are policy-checked. Give your model only `ctx.visible_tools()`.
3. **Add your tests** next to the existing ones. Reuse the `desk` fixture in `tests/conftest.py`: it builds a full
   in-memory desk (keys, bus, dispatcher, fake pager and gates), so a seat test is a few lines.

The example and the rules the runtime enforces are in [core/README.md](core/README.md#writing-a-seat-persons-2-to-5).

**Changing a contract** (a message type, the manifest, or the catalogue): change the Pydantic model in
`core/kcd_core/contracts`, run `python scripts/export_schemas.py`, update the fixtures, and tell Person 1. Adding
an optional field is fine; renaming or removing one breaks other people's seats. Changes to refusals or gates in
`contracts/policy_catalog.json` need a clinician's review.

### The 10 message types

`symptom_report`, `medication_report`, `red_flag_escalation`, `data_request`, `data_delivered`, `imaging_signal`,
`therapy_conflict`, `case_pack_ready`, `clinician_decision`, `acknowledgment`. Field names match the demo
(its golden-path messages validate here). Fields are in `core/kcd_core/contracts/messages.py`; examples are in
`contracts/fixtures/`. Payloads are strict: an undeclared field is rejected, which closes the demo's
`verification.passed` shortcut past the Safety Verifier.

Main flows as wired in the seed manifests:

- **Emergency (deck case 1):** Companion sends `red_flag_escalation` to Case Briefer. The Dispatcher pages the
  on-call doctor and escalates (primary, backup, head of unit, emergency desk) until someone acknowledges.
- **Drug conflict (case 2):** Companion sends `medication_report` to Renal Guardian, which sends `therapy_conflict`
  to Safety Verifier, which sends it to Case Briefer. Case Briefer sends `case_pack_ready`, and the Dispatcher
  opens a review gate for the doctor.
- **CT result (case 3):** Imaging Reader sends `imaging_signal` to Safety Verifier, which sends it to Case Briefer.
  The radiologist then signs.
- **Genomics:** Genomics Interpreter sends its result to Safety Verifier, then Case Briefer. (The demo sends it
  straight to the Case Briefer; we route it through the verifier on purpose.)

Every clinical output goes through the Safety Verifier. The Dispatcher rejects a route that skips it.

## Status

**Built (Core, week 1):**

- Contracts frozen as a first version: manifest, envelope, the 10 message types, policy catalogue, clinician release
- Policy enforcement point: deny by default; refusals win even over a listed tool; release-gated content
  (diagnosis, results, doses) needs a valid clinician signature for that exact artifact
- Ed25519-signed messages; the sender's signature is verified by the Dispatcher and again by the receiver
- Dispatcher: routing from manifests, deduplication, retry and dead-letter, gate opening
- Escalation ladder with on-call rota and pager interfaces; a redelivered red flag pages only once
- Manifest hot-reload and rollback; hash-chained audit log
- Contracts aligned to Andrea's v2 demo: its catalogue, its 8 seat manifests (imported), its field names
- Chief of Staff safety floor ported from the demo compiler (EN/IT/FR rules); an LLM draft can add refusals
  and gates but never drop one
- 9 manifests, 116 tests, CI workflow

**Next (Core):**

- NATS JetStream bus + Postgres outbox; Postgres stores for audit, deduplication and escalations
- MCP connector template, with `oncall` and `fhir` reference servers (others copy the template)
- Chief of Staff: LLM draft step behind `enforce_floor`, Director approval API, manifest diff for the console
- Escalation worker on Postgres; real paging behind a feature flag

## Open decisions

1. ~~Message types need checking against the demo.~~ Resolved: Core follows the v2 demo's vocabulary (see
   `core/README.md`, decisions 3 to 10).
2. **Own runtime vs Loop Nexus / Orchestra.** We built a thin runtime of our own. Everything external sits behind
   an interface (bus, keys, audit, stores, rota, pager, gates, tools), so a platform can replace it later without
   changing seat code. This needs a decision before the runtime grows.
3. **Models and jurisdiction** (from the plan): which edge models, whether a cloud model is allowed for
   de-identified data, and EU vs Morocco regulatory scope.
