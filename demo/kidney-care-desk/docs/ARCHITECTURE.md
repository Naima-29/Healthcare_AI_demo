# Architecture — Kidney Care Desk v2

Mapping of the deck ("Towards a New Medicine" v14) to the code.

| Slide | Concept | Code |
|---|---|---|
| 17 | v1 → v2 | the v1 pieces are wrapped, not rewritten: Telegram bot → `channel.mjs`; CT classifier → `mcp/servers.mjs#imagingServer` (`CT_MODEL_URL`); KLM/DNA/EHR → `fhir` + `genomics` + `knowledge/km.mjs` |
| 18 | Actors end to end | `desk.mjs` (assembly), `runtime/bus.mjs` (hub-and-spoke through the Dispatcher), `mcp/` (hospital systems) |
| 19 | One human, one door | `desk.mjs#doors` — `director` → `ChiefOfStaff`, `patient` → `Companion`, `clinician` → `Board` |
| 20 | Eight seats | `seats/*.job.md` + `seats/*.manifest.json`; behaviour in `backend/seats/` |
| 21 | NL job description → running agent | `compiler.mjs` (`amend`, `compileNew`) → `contracts/manifestSchema.mjs` → `runtime.hotReload()` |
| 22 | Worked example manifests | `seats/companion.manifest.json`, `seats/imaging_reader.manifest.json` |
| 23 | Where each technology lives | Humanized: `humanized/`; Agentic: `seats/`; SLM: `models/gateway.mjs`; KM: `knowledge/km.mjs`; Edge: Companion `location: patient_edge`; Memory: `memory/cortex.mjs`; Responsible AI: `runtime/guard.mjs`, `runtime/board.mjs` |
| 24 | Cortex memory L0–L3 | `memory/cortex.mjs` (evidence hash chain, knowledge shadow→confirmed, decay, erase); write scope enforced in `runtime/seat.mjs` |
| 25 | MCP + A2A | `mcp/protocol.mjs` (JSON-RPC 2.0), `bin/mcp-stdio.mjs`, `POST /mcp/:server`; `contracts/a2aSchema.mjs` (10 types, Ed25519), `GET /a2a/:seat/.well-known/agent-card.json` |
| 26 | Patient & dataset | `data/marco-r/` |
| 27 | Three test cases | `seats/companion.mjs` (red flag), `seats/clinical.mjs` (Renal Guardian, Imaging Reader, Safety Verifier) |
| 28 | Golden path | `scenario/goldenPath.mjs` (UI, CLI and e2e test use the same script) |
| 29 | Human gates | `runtime/board.mjs` (signed human acts, releases), `contracts/catalog.mjs#GATES` |

## Message flow (hub and spoke)

```
Companion ──► Dispatcher ──► { Case Briefer, Renal Guardian, Genomics Interpreter, Archivist, Safety Verifier }
   ▲              │  verify Ed25519 + schema, ack, route, page on-call, escalation timer
   └──────────────┘  (data_request, clinician_decision)
imaging_signal / therapy_conflict  →  Safety Verifier  →  (verified)  →  Case Briefer  →  Clinical Board
clinician signs on the Board  →  release (signed)  →  Case Briefer emits clinician_decision  →  Companion + Archivist
```

Only the Dispatcher may address a seat; every other seat can only send to the Dispatcher (`Bus.send` enforces it).

## How refusals are enforced

`manifest.refuses[]` → `contracts/catalog.mjs#REFUSALS` → each refusal blocks **actions** (checked by `Seat.act()` at the tool layer) and **content classes** (checked on every patient-facing message by `Seat.content()` → `guard.checkContent`). A content class can only pass with a **verified clinician release** (`Board.registerRelease` verifies the clinician's Ed25519 signature). Refusals found by the rule pass of the compiler form a safety floor: an SLM can add skills, never remove a refusal.

## Defence in depth for imaging

1. The Imaging Reader refuses `report_to_patient`, so it cannot send to the patient.
2. The Dispatcher routes `imaging_signal` only to the Safety Verifier and then to the Case Briefer, never to the Companion.
3. The Companion refuses `disclose_imaging_before_review` (added live by the Director).
4. The content classifier blocks imaging results in any patient message without a signed release.

## Production path

| Demo | Production |
|---|---|
| in-process bus | Redis Streams / NATS; keep `send()` / `subscribe()` |
| KeyRing in memory | per-seat keys in an HSM / Vault; JWS-signed Agent Cards |
| Runtime class | Loop Nexus / OpenClaw runtime; manifests unchanged |
| JSON files in Cortex | LanceDB / memory-wiki + per-hospital LoRA adapters |
| rules + optional SLM | Loop Q clinical SLMs on the edge node, distilled from the cloud teacher |
| Board in the SPA | Clinical Board integrated into the hospital's clinician workspace (SSO, audit) |
