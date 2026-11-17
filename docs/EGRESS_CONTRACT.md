# Cloud egress contract (C6) — DRAFT for review by the Safety Verifier owner

**Rule (plan, principle 7):** identifiable data never leaves the hospital. Core sends to a cloud model only a
payload the Safety Verifier has approved, and only through one code path: `kcd_core.egress.EgressGate`.

## What the Safety Verifier produces: `EgressApproval`

Sample approval (signature shortened):

```json
{
  "id": "appr_7f3a91c2",
  "decision": "approved",
  "payload_hash": "sha256:9f2c4e0b...a1b7",
  "target": "kimi",
  "approved_by": "safety_verifier",
  "requesting_seat": "case_briefer",
  "manifest_version": "case_briefer@v1",
  "issued_at": "2026-11-16T10:00:00Z",
  "expires_at": "2026-11-16T10:05:00Z",
  "reason": null,
  "sig": "k3Jd8...=="
}
```

Sample rejection (the verifier must send this, not silence, when anonymisation is not good enough):

```json
{
  "id": "appr_7f3a91c3", "decision": "rejected", "payload_hash": "sha256:9f2c4e0b...a1b7",
  "target": "kimi", "approved_by": "safety_verifier", "requesting_seat": "case_briefer",
  "manifest_version": "case_briefer@v1", "issued_at": "2026-11-16T10:00:00Z",
  "expires_at": "2026-11-16T10:05:00Z", "reason": "possible patient name in free text", "sig": "..."
}
```

Model: `core/kcd_core/contracts/egress.py` (JSON Schema: `contracts/schemas/egress_approval.schema.json`).

## How to compute it (both sides must match exactly)

- `payload_hash` = `content_hash(payload)` from `kcd_core.audit`: SHA-256 of the RFC 8785 canonical JSON of the
  exact dict that will be sent, written `sha256:<64 hex>`. Any change to the payload, even whitespace inside a
  string, changes the hash.
- `sig` = Ed25519 over the RFC 8785 canonical JSON of the approval without `sig`, signed with the
  `safety_verifier` key. Use `kcd_core.egress.sign_approval`; do not re-implement it.
- Times are timezone-aware and are normalised to UTC.

## What the gate checks (any failure blocks and is audited with a reason code)

| Check | Reason code |
|---|---|
| decision is `approved` | `rejected_by_verifier` |
| signed by `safety_verifier` (no other key, not even another seat's) | `wrong_approver`, `bad_signature` |
| `requesting_seat` and `target` equal what is being sent | `wrong_seat`, `wrong_target` |
| seat's manifest allows that cloud target (`model.fallback`) | `seat_not_allowed_cloud` |
| seat's manifest unchanged since approval | `stale_manifest` |
| not expired, not future-dated, validity <= 10 minutes | `expired`, `not_yet_valid`, `ttl_too_long` |
| hash of the bytes being sent equals `payload_hash` | `payload_changed` |
| none of the known patient identifiers appears in the payload (last tripwire) | `identifier_in_payload` |
| approval not used before (single use) | `already_used` |
| anything unexpected | `internal_error` (fails closed) |

The audit trail records hashes and reason codes, never the payload.

## Questions to agree in the meeting

1. Is the hash input exactly the dict the seat sends, and who builds it (seat or verifier)?
2. Is 5 minutes a sensible validity, and is single use acceptable for batch cases?
3. Target naming: the manifest says `"kimi (de-identified only)"`; the gate uses the part before the bracket
   (`kimi`). Clean the manifest to a plain id?
4. Rejection reasons: free text, or a fixed list (name, date of birth, address, free text, image text...)?
5. **Transport:** how does the approval travel from the verifier to the seat? Probably two new A2A message types
   (`egress_request`, `egress_decision`). Not built yet; this changes the message catalogue.
6. Where do `known_identifiers` (patient id, name, record numbers) come from at send time?

## Not built yet

An HTTP endpoint or real `CloudClient` for a specific provider, blocking the seats' own internet access so Core is
the only way out, and the A2A message types in question 5.
