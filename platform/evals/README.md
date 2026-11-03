# evals: evaluation harness (KCD-024)

Scores a seat function or service against a versioned dataset, prints a report, and fails CI when a threshold is missed.

Owner: ML engineer. Plan: [docs/PHASE0_TICKETS.md](../../docs/PHASE0_TICKETS.md), ticket KCD-024.

## Run it

From `platform/` (Python 3.12, standard library only):

```bash
python -m evals list
python -m evals validate red_flags
python -m evals run red_flags --target rules_baseline
python -m evals run red_flags --target rules_baseline --gate
python -m unittest discover -s tests
```

When `uv` is set up for the repo (KCD-001), the same commands run as `uv run python -m evals ...`.

Reports are written to `platform/reports/<dataset>/<target>-<timestamp>.md` and `.json`.

`--gate` exits with code 1 if any threshold in the dataset card fails. CI runs without `--gate` while a dataset is still a draft (`review_status` is not `"reviewed"`).

## Dataset format

One folder per dataset in `evals/datasets/<name>/`:

- `card.json`: metadata. `name`, `version`, `task`, `labels`, `source`, `labeller`, `review_status`, `created`, `thresholds`.
- `data.jsonl`: one item per line. `id`, `lang`, `text`, `context`, `label`, `tags`, `notes`.

Items tagged `critical` must never be missed. They are scored separately as `critical_recall`.

## Targets

A target is a function `item -> prediction`, registered with `@target(name, task)` in `evals/targets.py`.

| Target | What it is |
|---|---|
| `rules_baseline` | The demo's keyword rules (`backend/humanized/nlu.mjs` + `backend/knowledge/km.mjs#redFlags`), ported to Python. This is the floor the model has to beat. |
| `edge_llm` | Any OpenAI-compatible endpoint (`EDGE_LLM_BASE_URL`, `EDGE_LLM_MODEL`). Skipped if not configured. Used for KCD-020 model selection. |

For the `red_flag` task, a prediction is `{"urgency": "none" | "medium" | "high", "score": 0..1, "evidence": [...]}`.

## Metrics

`metrics.py`: per-class precision / recall / F1, macro F1, accuracy, confusion matrix, binary "must escalate" (urgency `high`) precision / recall, critical recall, AUROC and expected calibration error (ECE) on the `score`.

## Status

- `red_flags` v0.1: 32 messages in EN / IT, **draft labels written by ML, not yet reviewed by the clinical lead** (blocker X5). Don't treat the numbers as clinical evidence until `review_status` is `"reviewed"`.
- The 3 demo scenarios become end-to-end cases once the seats exist (Phase 2–3).
