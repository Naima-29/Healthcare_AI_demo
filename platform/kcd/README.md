# Evidence + Database slice (Naima)

Owner of: **Imaging Reader**, **Genomics Interpreter**, **Loop Cortex memory** and the **Archivist** (back end).
Deck slides 20, 22, 24, 26, 27 (case 3). Work division: the team's *healthcare-Demo* doc.

Python 3.12. Dependencies: `numpy`, `pillow`, `sqlalchemy` (optional: `pydicom` for DICOM key images, `psycopg` for Postgres).

## Run it

From `platform/`:

```bash
python -m unittest discover -s tests -t .        # 96 tests (whole platform)
python -m kcd.demo                               # case 3 + learning loop + erasure on Marco R.
python -m kcd.demo --db sqlite:///cortex.db --blobs ./blobs
python -m evals run genomics_relevance --target genomics_rules --gate
```

The demo uses a **stub CT model** (the v1 cached result) until Chrislin's model is connected; it says so in its output.

## What is in here

| Path | What it does |
|---|---|
| `cortex/store.py` | **Cortex**: evidence store (append-only, sha256 hash chain per partition, blobs) and knowledge store (versioned rules, trial → confirmed by a human, decay with half-life, re-verify, retire, supersede). Erasure of a patient partition with an anonymous receipt and hooks. SQLite now, Postgres by changing the URL. |
| `cortex/access.py` | `CortexView`: a seat's access, limited to the memory layers in its manifest. No confirm / reject / erase methods: those are human-only. |
| `cortex/blobs.py` | Content-addressed blob store (file or memory). Folder names are hashes, never patient ids. MinIO later behind the same methods. |
| `seats/archivist.py` | Signed `clinician_decision` → evidence → trial rules (L3 patient, L2 hospital) → `rule_confirmation` gate. `rules_for_next_episode()` returns confirmed, still-confident rules. |
| `seats/imaging_reader.py` | PACS → key image as evidence → quality check → CT model → calibration → OOD check → heat-map overlay → `imaging_signal` to the Dispatcher → `radiologist_review` gate. Unreadable image or model failure still produces a signal. |
| `imaging/` | Image decoding (PNG / JPEG / 16-bit / DICOM with pydicom), quality and OOD checks, temperature scaling (fit + ECE), heat-map metrics and overlay, CT model adapters, read-only PACS (folder, DICOMweb). |
| `seats/genomics_interpreter.py` | Panel → evidence → variant rules → `data_delivered{genomic_context}` → `genetic_disclosure` gate. Blocks a panel filed under the wrong patient. |
| `genomics/` | Panel parsing (demo JSON and VCF 4.x), the variant rule table (`variant_rules.json`) and interpretation. |
| `seatkit.py`, `contracts.py` | **Provisional** stand-ins for Core: manifest loading, refusal checks, diagnosis-text guard, outbox / gate / audit ports, message validation. |
| `manifests/` | Manifests for the three seats (job description, tools, memory layers, refusals, gates). |
| `../evals/datasets/genomics_relevance` | Sample set for scoring the variant rules (18 panels, draft labels). |

## Refusals and gates, and where they are enforced

| Seat | Refusal / gate | Enforced by |
|---|---|---|
| Imaging Reader | `label_as_diagnosis` | fixed note + `is_diagnosis_claim` text guard on every outgoing text |
| | `report_to_patient` | only peer is the Dispatcher; `send(to="patient")` is blocked |
| | `alter_image` | PACS sources have no write method; key image stored as evidence with its sha256 |
| | gate `radiologist_review` | opened for every signal, including unreadable / model error |
| Genomics Interpreter | `reclassify_variants` | lab class copied verbatim; checked against the panel before sending |
| | `disclose_genetics_to_patient`, `send_to_patient` | only peer is the Dispatcher |
| | gate `genetic_disclosure` | opened for every interpretation |
| Archivist | `store_without_evidence` | Cortex refuses a rule without evidence in the same partition; unsigned decisions are refused |
| | `auto_confirm_rule` | the Archivist's view has no confirm; Cortex confirm needs a verified human |
| | gate `rule_confirmation` | opened for every trial rule |

A gate that can't be opened (no gate service) raises an error: it is never skipped.

## For the team: where your work plugs in

**Harith (Core).** The seats take three ports: `outbox(envelope)` (A2A bus), `gates(gate, patient, title, artifact, opened_by)` (Clinical Board) and an optional audit sink. Swap in the real ones; `seatkit.py` and `contracts.py` then shrink to imports of your runtime and frozen schemas. Also replace `default_human_verifier` (Cortex) and `default_decision_verifier` (Archivist) with checks of the clinician's signed release. Contract points to settle with you:
- `imaging_signal` has a `status` (`ok`, `out_of_distribution`, `unreadable`, `model_error`); without a model result, `label` is `"indeterminate"` and `p_calibrated` is `null`.
- Seats set `route_hint` (Imaging → Safety Verifier + Case Briefer; Genomics → Case Briefer); the Dispatcher decides.

**Chrislin (CT model, Board, Memory panel).**
- Deliver the model as an HTTP service or a Python function returning:
  `{"model_version": "...", "labels": ["Normal","Cyst","Stone","Tumor"], "logits": [...] or "probs": [...], "heatmap": 2-D array or "heatmap_png_b64": "..."}` (labels in any order; Kaggle names are fine). HTTP: `POST {"image_b64", "media_type"}`.
- Send the validation-set logits and labels: `Calibration.fit(logits, labels, model_version).save("cal.json")` fits the temperature. A calibration for another model version is ignored and the signal says `stale`.
- Board: heat-map overlay PNG is evidence `heatmap_ref` (read with `cortex.read_blob`). Memory panel: `list_knowledge(..., status="shadow")`, then `confirm`, `reject`, `reverify`, `retire`, `erase_patient` with the logged-in `HumanActor`.

**Diana (Knowledge Model, Safety Verifier).** `genomics/variant_rules.json` is the variant-rule content to merge into the KM (each rule has a relevance, summary and citations). The Safety Verifier receives `imaging_signal` with calibrated confidence, quality checks and provenance.

**Meet (Companion).** `Archivist.rules_for_next_episode(patient)` returns the confirmed rules (e.g. avoid NSAIDs) for the next conversation.

## Not done or not verified yet

- **Postgres** is supported through SQLAlchemy but only tested on SQLite (no Postgres server here).
- **DICOM key images** need `pydicom`; that path is written but not exercised (pydicom isn't installed here). DICOMweb is tested against a local fake server, not a real Orthanc.
- **The CT model** is a stub until Chrislin delivers; the demo's temperature 2.5 is a placeholder.
- **Labels and rules are drafts**: `variant_rules.json` and the `genomics_relevance` labels need a geneticist; the genomics score is partly circular until then.
- `seatkit.py` / `contracts.py` are provisional until Core freezes the runtime and contract C2.
- No real multi-process deployment yet: the A2A bus and gate service are in-memory stand-ins.
