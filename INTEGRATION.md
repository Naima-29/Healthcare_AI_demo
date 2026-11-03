# `dev`: integration of all branches

`dev` merges every branch pushed so far into one working desk, following the engineering plan: the real system is
Python on Core (`core/kcd_core`), and the Node v2 demo stays the stage demo and the reference for the vocabulary.

## What is merged

| Branch | Owner | What it brings | How it landed in `dev` |
|---|---|---|---|
| `agent-1-core` (0e359f9) | Harith (Core) | kcd_core: contracts aligned with the demo, PEP, signed bus, Dispatcher + escalation, registry, Chief of Staff safety floor; the v2 demo under `demo/` | base of `dev` |
| `diana/agent-4` | Diana (Clinical reasoning) | Data-driven Knowledge Model (`rules.json`), Safety Verifier "every citation exists" check, 23 seat tests (Node) | merged; files moved into `demo/kidney-care-desk/` where they run; rules ported to Python (below) |
| `naima/evidence-on-core` (incl. `naima/evidence-database`) | Naima (Evidence + Database) | Cortex memory, Imaging Reader, Genomics Interpreter, Archivist, evaluation harness | merged; re-ported to Core's new contracts |

No branch yet from Meet (Companion) or Chrislin (Case Briefer, Clinical Board, CT model). The golden path uses
clearly marked stand-ins for those two seats (`platform/kcd/standins.py`) and the v1 cached CT result (stub model).

## Run it

```bash
pip install -e . numpy pillow sqlalchemy pytest     # or: uv sync, then pip install numpy pillow sqlalchemy
pytest -q                                           # Core: 117 tests
cd platform
python -m pytest -q -p no:asyncio                   # seats, Cortex, Knowledge Model, golden path, edge cases
python -m kcd.demo                                  # the golden path, deck slide 28, steps 1 to 8
python -m kcd.scenarios.edge_cases                  # every edge case for one patient (Sofia B.): 52 checks
cd ../demo/kidney-care-desk && npm start            # the Node stage demo (UI), http://localhost:5173
```

The Python golden path runs on kcd_core with real seats: Dispatcher + escalation (Core), Renal Guardian +
Safety Verifier + Knowledge Model (Diana's rules), Imaging Reader + Genomics Interpreter + Archivist + Cortex
(Naima). 15 messages, all signed, routed and policy-checked; no dead letters; the clinician signs the case pack
and confirms the trial rules with Ed25519 releases.

## Discrepancies found, and what `dev` does about them

| # | Discrepancy | Resolution in `dev` | Owner to confirm |
|---|---|---|---|
| 1 | Core's contracts changed after `naima/evidence-on-core` was built (catalogue 2.0.0, demo-format manifests, new message fields, memory layers 0-3) | Core's contracts win every conflict; Naima's seats re-ported to them; her catalogue 1.1.0 additions dropped (2.0.0 already has `alter_image`, `reclassify_variants`, `auto_confirm_rule`, `write_general_weights`, `store_without_evidence`) | Naima |
| 2 | Diana's branch is Node at the repo root (`backend/`, `tests/*.mjs`), but `backend/` only exists inside the demo, and `tests/` is Core's Python folder | Her files moved into `demo/kidney-care-desk/` (all 40 demo tests pass); her rules ported to Python on Core (`platform/kcd/knowledge`, `renal_guardian.py`, `safety_verifier.py`), with her behaviour tests translated (`platform/tests/test_clinical.py`) | Diana |
| 3 | Core's README says `demo/` is "kept as received, do not edit", and Diana's work edits demo files | Kept her edits in the demo, since that is where they run and they improve it; flagged here | Harith, Diana |
| 4 | The Knowledge Model data existed only inside the demo | Canonical copy in `knowledge/rules.json` (plus `knowledge/variant_rules.json`, moved from the genomics package); a test fails if the demo's copy drifts | Diana |
| 5 | The demo's Safety Verifier stamps `verification: {passed: true}` into payloads; Core's strict payloads reject that field on purpose | Python verifier forwards the payload unchanged (its signature is the proof) and returns a rejected acknowledgment with the failed checks | Diana |
| 6 | Diana's Node tests assert `a2a_peers == ['dispatcher']` (demo routing); in Core `a2a_peers` names the receiving seats | Python tests check Core's routes instead (`renal_guardian -> companion, safety_verifier`) | Diana |
| 7 | `ImagingSignal` requires a label and a calibrated probability, so an unreadable image or a failed model can't be signalled | Recorded as evidence + an audit error (`imaging.no_signal`) and returned to the caller; nothing silent. Proposal: allow a "no result" signal | Harith |
| 8 | Genomics needs the patient's history (VHL + RCC history -> kidney-sparing note), but its seed manifest has no FHIR access | Reviewed override in `scripts/import_demo_manifests.py`: `fhir.get_conditions` + surface `fhir` | Harith |
| 9 | Evidence and rules share a memory layer, so a memory check alone would let any seat with layer-3 write propose rules | `CoreMemory` also requires the catalogue tool `cortex.knowledge_propose` (only the Archivist holds it) | Harith |
| 10 | The Archivist can't verify the clinician's Ed25519 release itself: `ClinicianDecision` carries `decision_id` and `signature` but not the full `Release` | The Case Briefer verifies the release against the case pack hash before publishing; the Archivist trusts the routed, signed decision and checks it names a person with a clinical role | Harith, Chrislin |
| 11 | "NSAIDs" (plural) is not recognised as an NSAID: the names list has only the singular (same in Node and Python) | Left as is; a test documents it | Diana |
| 12 | Ownership table in the root README differed from the team doc (Person 3 / Person 5 swapped) | Table aligned with the team doc | Harith |
| 13 | The panel source read every `.json` in the patient folder, so a FHIR bundle next to the panel broke it | Fixed: only `panel*.json` and `*.vcf` | Naima |
| 14 | One red flag sent to two seats (the Companion's route: Case Briefer + Archivist) opened two escalations and paged the on-call doctor twice (found by the edge-case scenario) | Fixed in `core/kcd_core/dispatcher.py`: one escalation per sender + episode + red flag, whatever the receiver; regression test in `tests/test_dispatcher.py` | Harith |

## Not done yet

- Companion (Meet) and Case Briefer / Clinical Board / CT model (Chrislin): stand-ins and a stub until their branches land.
- The Python desk has no UI yet; the Node stage demo has one.
- Postgres, NATS, MCP servers: in-memory and local adapters behind Core's ports.
- All clinical content (`knowledge/*.json`, red-flag set, eval labels) is a draft that needs clinical review.
