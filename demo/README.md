# Demo v2 (reference only)

`kidney-care-desk/` is the v2 stage demo as Andrea handed it over (Node >= 20, no dependencies):

```bash
cd demo/kidney-care-desk
npm start      # http://localhost:5173
npm test
```

**Do not edit it.** It is the spec and the stage demo, not the runtime. The real system is Core (`core/`) and the
seats built on it. What Core takes from here:

| From the demo | Into Core |
|---|---|
| `contracts/catalog.mjs` (refusals, gates, surfaces) | `contracts/policy_catalog.json` |
| `contracts/manifestSchema.mjs` | `core/kcd_core/contracts/manifest.py` |
| `contracts/a2aSchema.mjs` (10 message types) | `core/kcd_core/contracts/messages.py` |
| `seats/*.manifest.json`, `seats/*.job.md` | `seats/manifests/`, `seats/jobs/` via `scripts/import_demo_manifests.py` |
| `backend/compiler.mjs` rule pass | `core/kcd_core/chief_of_staff/safety_floor.py` |

Why the demo runtime is not built on: see the review doc linked from the root README.
