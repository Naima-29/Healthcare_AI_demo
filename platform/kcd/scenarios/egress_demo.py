"""Cloud egress demo: the Safety Verifier de-identifies a case summary and signs an approval; Core's egress gate
sends only that exact, approved payload to the (fake) cloud model. Synthetic data only.

    python -m kcd.scenarios.egress_demo
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from kcd_core.egress import EgressBlocked, EgressGate, InMemoryApprovalUses

from kcd.deidentify import Identifiers
from kcd.desk import Desk
from kcd.fhir import FolderFhir
from kcd.seats.safety_verifier import SafetyVerifier

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
SEAT, TARGET = "case_briefer", "kimi"
CASE = {
    "case_id": "case-marco-r-1",
    "patient": {"name": "Marco R.", "birthDate": "1978-03-14", "telecom": "+39 333 123 4567"},
    "summary": "Marco R. (born 14/03/1978, MRN-0048213), Via Roma 12, Milano. Phone 333 123 4567, "
               "marco.r@example.com. Seen by Dr. G. Ferri on 2026-10-08. "
               "eGFR 38 and falling, 3.1 cm renal mass, takes ibuprofen 600 mg.",
}


class FakeCloud:
    async def send(self, target: str, payload: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True, "received_by": target}


def show(title: str, value: Any) -> None:
    print(f"\n{title}\n" + json.dumps(value, indent=2, ensure_ascii=False))


async def main() -> None:
    desk = await Desk.create(start=datetime(2026, 11, 24, 10, 0, tzinfo=UTC))
    verifier = SafetyVerifier(now=desk.clock.now)
    gate = EgressGate(registry=desk.registry, keyring=desk.seat_keys, client=FakeCloud(),
                      uses=InMemoryApprovalUses(), audit=desk.audit, clock=desk.clock)
    ids = Identifiers.from_patient(FolderFhir(FIXTURES).patient("marco-r"), extra=["MRN-0048213"])

    def approve(payload: dict[str, Any]) -> tuple[dict[str, Any], Any]:
        return verifier.approve_egress(payload, identifiers=ids, target=TARGET, requesting_seat=SEAT,
                                       manifest_version=desk.registry.get(SEAT).ref,
                                       signer=desk.signers["safety_verifier"])

    async def attempt(label: str, payload: dict[str, Any], approval: Any) -> None:
        try:
            await gate.send(seat=SEAT, target=TARGET, payload=payload, approval=approval, known_identifiers=ids.all())
            print(f"  {label:<55} SENT")
        except EgressBlocked as blocked:
            print(f"  {label:<55} BLOCKED ({blocked.reason})")

    show("1. Case summary inside the hospital (identifiable):", CASE)
    clean, approval = approve(CASE)
    show("2. After the Safety Verifier's de-identification:", clean)
    print(f"\n3. Signed decision: {approval.decision}, valid until {approval.expires_at:%H:%M} UTC, "
          f"bound to {approval.payload_hash[:19]}...")

    print("\n4. Core's egress gate:")
    await attempt("de-identified payload with its approval", clean, approval)
    await attempt("same approval used a second time", clean, approval)
    _, fresh = approve(CASE)
    await attempt("ORIGINAL (identifiable) payload with an approval", CASE, fresh)
    leaky_clean, leaky = approve({"marco-r": {"egfr": 38}})
    print(f"     (verifier decision on a payload keyed by the patient id: {leaky.decision}: {leaky.reason})")
    await attempt("payload the verifier rejected", leaky_clean, leaky)
    late_clean, late = approve(CASE)
    desk.clock.advance(timedelta(minutes=6))
    await attempt("approval used after it expired (6 minutes)", late_clean, late)

    sent, blocked = desk.audit.find("egress.sent", "allowed"), desk.audit.find("egress.blocked", "blocked")
    print(f"\n5. Audit trail: {len(sent)} sent, {len(blocked)} blocked; "
          "it holds hashes and reasons, never the payload.")
    await desk.close()


if __name__ == "__main__":
    asyncio.run(main())
