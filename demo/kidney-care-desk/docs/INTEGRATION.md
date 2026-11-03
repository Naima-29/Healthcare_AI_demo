# Integration: from v1 (90%) to v2

## 1. The v1 CT classifier → `CT_MODEL_URL`

The `imaging` MCP server calls the model with this contract:

```http
POST $CT_MODEL_URL
{ "image_url": "<URL of the key image from PACS>" }

200 OK
{ "model": "kidney-ct-cls v1", "label": "tumor", "confidence": 0.9999,
  "cam_metrics": { "coverage": 0.0322, "center_ratio": 0.0495, "lr_asymmetry": 0.5446, "tb_asymmetry": 1.0 },
  "heatmap_url": "https://…/heatmap.png" }
```

If the v1 pipeline returns a different shape, adapt `imagingServer()` in `backend/mcp/servers.mjs` (about 15 lines).
The raw confidence is **calibrated** (temperature scaling, `CALIBRATION_T`) before it reaches the Board: 0.9999 → 0.975. Estimate T on your validation set.

## 2. The v1 Telegram bot → `TELEGRAM_BOT_TOKEN`

With the token set, `channel.mjs` long-polls `getUpdates`, and real messages go into the Companion (`doors.patient.say`). The Companion's replies go both to the phone in the UI and to Telegram. If the v1 bot uses a webhook, remove it first (`deleteWebhook`), because `getUpdates` does not work while a webhook is active.

## 3. Loop Orchestra: fixing the pipeline

The v1 pipeline was a single chain. In v2 each node becomes a seat with a manifest, and the chain becomes a sequence of typed A2A messages:

| v1 pipeline node | v2 seat | Outgoing message |
|---|---|---|
| Patient Agent (chat + questions) | Companion | `symptom_report`, `red_flag_escalation`, `medication_report` |
| KLM on DNA | Genomics Interpreter | `data_delivered{genomic_context}` |
| KLM on EHR + SLMs | Renal Guardian / Case Briefer | `therapy_conflict`, `case_pack_ready` |
| Imaging model + heat map | Imaging Reader | `imaging_signal` |
| Doctor Agent (next steps) | Case Briefer → Clinical Board | `case_pack_ready` → signature → `clinician_decision` |

Each seat can be tested on its own (see `tests/`). If one node breaks, the rest keeps working and the problem shows up in the stream.

## 4. Edge SLM (Loop Q) → `EDGE_LLM_*`

Any OpenAI-compatible endpoint works (Ollama, vLLM). Today the SLM is used to:
- propose skills when the Chief of Staff creates a new agent;
- write the case-pack narrative, from de-identified data.

All safety blocks stay deterministic: the SLM can add, never remove, a refusal.

## 5. Real FHIR / PACS

- `docker compose --profile live up -d`, then `FHIR_BASE_URL=http://localhost:8080/fhir node scripts/seed-live.mjs`.
- For Orthanc, upload a de-identified kidney CT (e.g. a KiTS19 case) with `PatientID=marco-r`.

## 6. Language

- `DEMO_LANG=en` (default) or `it` sets the scripted patient lines, the Director's sentence, the lab report and the draft wording for the patient.
- Agents always answer in the language they are addressed in.
- The compiler understands refusals written in English, Italian and French.

## 7. Checklist before the demo

- [ ] `npm test` green
- [ ] `DEMO_PACE=700` on stage (readable rhythm), `ESCALATION_SECONDS=90`
- [ ] run the whole golden path once, then **Reset**, before going on stage
- [ ] backup video recording (Autoplay)
- [ ] script and case pack reviewed by a nephrologist
