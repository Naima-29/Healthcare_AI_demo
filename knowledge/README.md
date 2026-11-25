# Knowledge Model data

Owner: Diana (Person 4). Language-neutral, reviewable data that the seats read; no code here.

| File | What | Read by |
|---|---|---|
| `rules.json` | Citations, drug classes, CKD stages (KDIGO), drug–disease conflict rules | `platform/kcd/knowledge/km.py` (Renal Guardian, Safety Verifier) |
| `variant_rules.json` | Variant relevance rules (ACMG context, APOL1 allele counting) | `platform/kcd/genomics/rules.py` (Genomics Interpreter) |

`rules.json` is also used by the Node stage demo, which keeps its own copy at
`demo/kidney-care-desk/backend/knowledge/rules.json`. A test (`platform/tests/test_knowledge.py`) fails if the two
copies differ, and checks that every citation used by `variant_rules.json` exists in `rules.json`, so the Safety
Verifier's "every citation exists in the Knowledge Model" check passes for genomic context.

Both files are **drafts**: `reviewed_by` is empty and every rule must be reviewed by a clinician (and the variant
rules by a geneticist) before any real use.
