# Evidence + Database slice (Naima), on kcd_core

Owner of: **Imaging Reader**, **Genomics Interpreter**, **Loop Cortex memory** and the **Archivist** (back end).
Deck slides 20, 22, 24, 26, 27 (case 3). Work division: the team's *healthcare-Demo* doc.

The seats run on Harith's Core (`core/kcd_core`): they subclass `kcd_core.runtime.Seat`, reach every tool
through `ctx.tool` (checked by the PEP and audited), every memory layer through `ctx.check_memory`, and send
only Core's frozen payloads with `ctx.send` (signed, routed by the Dispatcher). Their manifests are in
`seats/manifests/` at the repo root.

## Run it

From the repo root, once: `pip install -e . numpy pillow sqlalchemy pytest` (or `uv sync` plus those three).
Then from `platform/`:

```bash
python -m pytest -q -p no:asyncio                # 101 tests (pythonpath set in pyproject.toml)
python -m kcd.demo                               # case 3 + genomic context + learning loop + erasure
python -m kcd.demo --db sqlite:///cortex.db --blobs ./blobs
python -m evals run genomics_relevance --target genomics_rules --gate
```

Core's own checks are unchanged: `uv run pytest -q`, `uv run mypy`, `uv run ruff check .` at the root.
The demo uses a **stub CT model** (the v1 cached result) until Chrislin's model is connected; it says so.

## What is in here

| Path | What it does |
|---|---|
| `seats/imaging_reader.py` | PACS event → key image as evidence → quality check → `ct.classify` → calibration → OOD check → heat-map overlay → `imaging_signal` to the Safety Verifier. Called by a PACS watcher as `process_study(runtime.context(), ...)`. |
| `seats/genomics_interpreter.py` | `data_request` (genomics) from the Case Briefer → `genomics.read_panel` → wrong-patient check → `km.lookup` variant rules + `fhir.read_patient` → `data_delivered{genomic_context}` to the Safety Verifier. |
| `seats/archivist.py` | `clinician_decision` → `board.read_artifact` → verify the clinician's signed release and the artifact hash → evidence → trial rules → `case_pack_ready` per rule (the Dispatcher opens the confirmation gate) → acknowledgment. |
| `cortex/store.py` | **Cortex**: append-only, hash-chained evidence store with blobs; versioned knowledge store (trial → confirmed by a human, decay, re-verify, retire, supersede); patient erasure with an anonymous, optionally keyed receipt. SQLite now, Postgres by URL. |
| `cortex/access.py` | `CoreMemory`: a seat's memory access, checked by the PEP layer by layer (mapping below). No human-only operations. |
| `cortex/auth.py` | `release_verifier(clinician_keys)`: Cortex confirm / reject / re-verify / retire / erase only with the clinician's signed kcd_core `Release` for that exact rule (or `erase:<patient>`). |
| `tools.py` | Tool bindings (`pacs.*`, `ct.classify`, `genomics.read_panel`, `km.lookup`, `fhir.read_patient`, `board.read_artifact`) over local adapters, until the MCP servers exist. |
| `desk.py` | A whole desk in memory (registry, keys, bus, Dispatcher, gate recorder), built like Core's test fixture. Used by the tests and the demo. |
| `imaging/`, `genomics/` | Decoding, quality / OOD, calibration, heat map, CT model adapters, read-only PACS; panel parsing (JSON, VCF) and the variant rule table. |
| `textguard.py` | Flags text that reads as a diagnosis; seats then call `ctx.guard_content("diagnosis")`, which the PEP blocks without a release. |

### Memory layers: kcd_core names → Cortex

| kcd_core layer | Meaning (Core) | Cortex storage |
|---|---|---|
| L0 | episode working memory | idempotency bookkeeping, partition (3, patient) |
| L1 | patient evidence | evidence store, partition (3, patient) |
| L2 | patient knowledge | knowledge store, partition (3, patient) |
| L3 | shared hospital knowledge | evidence + knowledge, partition (2, hospital) |

Cortex keeps the deck's numbering internally (slide 24: 3 = patient, 2 = hospital). **Core's layer names differ
from the deck's** (deck: L0 medical knowledge, L1 specialty, L2 hospital, L3 patient). Worth settling with Core so
the Board and docs use one meaning.

## Refusals and gates

| Seat | Refusal (catalogue) | Enforced by |
|---|---|---|
| Imaging Reader | `no_diagnosis` | `textguard` + `ctx.guard_content("diagnosis")`; the fixed note passes the guard |
| | `no_patient_contact`, `no_result_disclosure` | no patient channel tool; only peer is the Safety Verifier |
| | `no_image_alteration` (new) | blocks `pacs.write_study`; adapters have no write method; key image stored with its sha256 |
| Genomics Interpreter | `no_variant_reclassification` | blocks `genomics.write_classification`; lab class verified unchanged before sending |
| | `no_patient_contact`, `no_result_disclosure` | only peer is the Safety Verifier |
| Archivist | `no_rule_self_confirmation` (new) | blocks `cortex.confirm_rule`; `CoreMemory` has no confirm; Cortex confirm needs a verified human |
| | `no_patient_data_in_weights` (new) | blocks `model.train_on_patient_data` |
| | store without evidence | Cortex invariant: a rule must cite evidence in its own partition |

Gates are declared in the manifests (`imaging_signal` → `clinician:radiology`, `genomic_context` →
`clinician:genetics`, `knowledge_rule` → `clinician:nephrology`). The Dispatcher opens them on `case_pack_ready`.

## Contract changes to raise with Harith (Core)

1. **ImagingSignal can't say "no result".** An undecodable image or a failed model has no label or probabilities,
   so today nothing is sent: the failure goes to evidence and an audit event (`imaging.no_signal`, decision
   `error`). Proposal: add `quality: "unreadable" | "model_error"`, make `label`, `p_raw`, `p_calibrated` optional
   when there's no result, so the radiologist's queue shows the study.
2. **ImagingSignal.heatmap_ref is required.** A model without Grad-CAM sends `"none"`. Proposal: make it optional.
3. **ClinicianDecision has no decision items.** The Archivist reads them from the released artifact with the new
   tool `board.read_artifact` and checks `content_hash(artifact) == artifact_hash`. Proposal: keep it that way, but
   agree on the artifact format with Chrislin (`{"items": [{"code", "patient_text", "rule"?}]}` today).
4. **Rule confirmation reuses `case_pack_ready`.** The Archivist sends one per trial rule so the Dispatcher's gate
   port opens a review item. Proposal: a generic review-item message, or accept `case_pack_ready` for any artifact.
5. **Memory layer meanings** differ from the deck (table above).
6. **Catalogue 1.1.0** adds tools `pacs.list_studies`, `pacs.write_study`, `board.read_artifact`,
   `cortex.confirm_rule`, `model.train_on_patient_data` and the three new refusals. Please review.
7. **Seat-level audit**: `SeatContext` has no public way to record an audit event, so the Imaging Reader takes the
   runtime's audit sink and clock in its constructor. A `ctx.audit(...)` would be cleaner.

## For the rest of the team

**Chrislin (CT model, Board, Memory panel).** Deliver the model as an HTTP service or a Python function returning
`{"model_version", "labels": ["Normal","Cyst","Stone","Tumor"], "logits" or "probs", "heatmap" or "heatmap_png_b64"}`.
Send validation logits and labels so `Calibration.fit(...)` can fit the temperature. Board: expose
`board.read_artifact(artifact_id) -> {"artifact", "release"}`; heat-map overlays are evidence (`heatmap_ref`).
Memory panel: list trial rules with `cortex.list_knowledge(..., status="shadow")`, then `confirm` / `reject` /
`reverify` / `retire` / `erase_patient` with a `HumanActor` carrying the clinician's signed `Release` for that rule.

**Diana (Knowledge Model, Safety Verifier).** `genomics/variant_rules.json` is the variant-rule content for the KM;
the seat already fetches it through `km.lookup(topic="variant_rules")`, so the KM can serve it as-is. The Safety
Verifier receives `imaging_signal` and `data_delivered{genomic_context}` (summary item first, then findings).

**Meet (Companion).** `Archivist.rules_for_next_episode(ctx, patient_id)` returns the confirmed rules
(for example, avoid NSAIDs) for the next conversation.

## Not done or not verified yet

- **Postgres** is supported through SQLAlchemy but only tested on SQLite.
- **DICOM key images** need `pydicom`; that path isn't exercised here. DICOMweb is tested against a local fake server.
- **The CT model** is a stub until Chrislin delivers; the demo's temperature 2.5 is a placeholder.
- **Labels and rules are drafts**: `variant_rules.json` and the `genomics_relevance` labels need a geneticist.
- The tool bindings are local adapters; the MCP servers (Core's template) replace them later.
