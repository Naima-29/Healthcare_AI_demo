"""The egress gate: nothing reaches a cloud model unless the Safety Verifier's signed approval matches exactly."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from kcd_core.adapters.pg_stores import PostgresApprovalUses
from kcd_core.audit import content_hash
from kcd_core.contracts import EgressApproval
from kcd_core.egress import (
    EgressBlocked,
    EgressGate,
    InMemoryApprovalUses,
    cloud_target_of,
    sign_approval,
    verify_approval,
)
from kcd_core.signing import SignatureError, Signer

SEAT, TARGET = "case_briefer", "kimi"
PAYLOAD = {"summary": "Male, 68, eGFR 38 and falling, 3.1 cm renal mass", "age_band": "65-74"}


class Cloud:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict]] = []
        self.fail = False

    async def send(self, target, payload):
        if self.fail:
            raise ConnectionError("cloud down")
        self.sent.append((target, payload))
        return {"ok": True}


@pytest.fixture
def world(desk):
    verifier = desk.signers["safety_verifier"]
    cloud = Cloud()
    gate = EgressGate(registry=desk.registry, keyring=desk.seat_keys, client=cloud, uses=InMemoryApprovalUses(),
                      audit=desk.audit, clock=desk.clock)

    def approval(**kw) -> EgressApproval:
        now = desk.clock.now()
        base = dict(id="appr_0001_aaaa", decision="approved", payload_hash=content_hash(PAYLOAD), target=TARGET,
                    approved_by="safety_verifier", requesting_seat=SEAT,
                    manifest_version=desk.registry.get(SEAT).ref, issued_at=now,
                    expires_at=now + timedelta(minutes=5))
        base.update(kw)
        return sign_approval(EgressApproval(**base), verifier)

    return gate, cloud, approval, desk


async def blocked(gate, reason, **kw):
    with pytest.raises(EgressBlocked) as info:
        await gate.send(**kw)
    assert info.value.reason == reason


def call(approval, **over):
    return {"seat": SEAT, "target": TARGET, "payload": PAYLOAD, "approval": approval, **over}


class TestHappyPath:
    async def test_matching_approval_sends_and_is_audited_without_the_payload(self, world):
        gate, cloud, approval, desk = world
        assert await gate.send(**call(approval())) == {"ok": True}
        assert cloud.sent == [(TARGET, PAYLOAD)]
        ev = desk.audit.find("egress.sent", "allowed")[0]
        assert ev.detail["payload_hash"] == content_hash(PAYLOAD) and ev.detail["target"] == TARGET
        assert "eGFR" not in str(ev.model_dump())  # hashes and reason codes only
        assert desk.audit.verify_chain()

    def test_target_id_is_read_from_the_manifest_note(self, desk):
        assert cloud_target_of(desk.registry.get(SEAT)) == "kimi"
        assert cloud_target_of(desk.registry.get("companion")) is None


class TestBlocks:
    async def test_payload_changed_after_approval(self, world):
        gate, cloud, approval, _ = world
        await blocked(gate, "payload_changed", **call(approval(), payload={**PAYLOAD, "summary": "Mario Rossi, 68"}))
        assert cloud.sent == []

    async def test_approval_is_single_use(self, world):
        gate, cloud, approval, _ = world
        a = approval()
        await gate.send(**call(a))
        await blocked(gate, "already_used", **call(a))
        assert len(cloud.sent) == 1

    async def test_concurrent_double_send_lets_exactly_one_through(self, world):
        gate, cloud, approval, _ = world
        a = approval()
        results = await asyncio.gather(gate.send(**call(a)), gate.send(**call(a)), return_exceptions=True)
        assert sum(isinstance(r, EgressBlocked) for r in results) == 1 and len(cloud.sent) == 1

    async def test_expired_and_not_yet_valid_and_overlong(self, world):
        gate, _, approval, desk = world
        a = approval()
        desk.clock.advance(timedelta(minutes=6))
        await blocked(gate, "expired", **call(a))
        now = desk.clock.now()
        await blocked(gate, "not_yet_valid", **call(approval(id="appr_0002_bbbb", issued_at=now + timedelta(hours=1),
                                                             expires_at=now + timedelta(hours=1, minutes=5))))
        await blocked(gate, "ttl_too_long", **call(approval(id="appr_0003_cccc", expires_at=now + timedelta(days=30))))

    async def test_rejection_by_the_verifier_never_sends(self, world):
        gate, cloud, approval, _ = world
        await blocked(gate, "rejected_by_verifier",
                      **call(approval(decision="rejected", reason="possible patient name in free text")))
        assert cloud.sent == []

    async def test_only_the_safety_verifier_may_approve(self, world):
        gate, _, approval, desk = world
        # a valid key, but not the verifier's: a seat cannot approve its own egress
        a = sign_approval(EgressApproval(
            id="appr_0004_dddd", decision="approved", payload_hash=content_hash(PAYLOAD), target=TARGET,
            approved_by=SEAT, requesting_seat=SEAT, manifest_version=desk.registry.get(SEAT).ref,
            issued_at=desk.clock.now(), expires_at=desk.clock.now() + timedelta(minutes=5)), desk.signers[SEAT])
        await blocked(gate, "wrong_approver", **call(a))

    async def test_forged_or_tampered_signature(self, world):
        gate, _, approval, desk = world
        good = approval()
        await blocked(gate, "bad_signature", **call(good.model_copy(update={"target": "other"}), target="other"))
        await blocked(gate, "bad_signature", **call(good.model_copy(update={"sig": None})))
        impostor = sign_approval(good.model_copy(update={"sig": None}),
                                 Signer("safety_verifier", Signer.generate("x").private_key))
        await blocked(gate, "bad_signature", **call(impostor))

    async def test_approval_cannot_be_borrowed_by_another_seat_or_target(self, world):
        gate, _, approval, _ = world
        a = approval()
        await blocked(gate, "wrong_seat", **call(a, seat="renal_guardian"))
        await blocked(gate, "wrong_target", **call(a, target="gpt-big"))

    async def test_seat_without_cloud_permission_is_refused_even_with_a_valid_approval(self, world):
        gate, cloud, approval, desk = world
        a = approval(requesting_seat="companion", manifest_version=desk.registry.get("companion").ref)
        await blocked(gate, "seat_not_allowed_cloud", **call(a, seat="companion"))
        assert cloud.sent == []

    async def test_manifest_change_invalidates_outstanding_approvals(self, world):
        gate, _, approval, desk = world
        a = approval()
        live = desk.registry.get(SEAT)
        desk.registry.activate(live.model_copy(update={"version": live.version + 1}))
        await blocked(gate, "stale_manifest", **call(a))

    async def test_identifier_tripwire(self, world):
        gate, cloud, approval, _ = world
        leaky = {**PAYLOAD, "note": "follow up with pt_marco next week"}
        a = approval(payload_hash=content_hash(leaky))
        await blocked(gate, "identifier_in_payload", **call(a, payload=leaky, known_identifiers=["pt_marco", "Rossi"]))
        assert cloud.sent == []

    async def test_unexpected_error_fails_closed(self, world):
        gate, cloud, approval, _ = world

        class Broken:
            def consume(self, *a):
                raise RuntimeError("db down")

        gate.uses = Broken()
        await blocked(gate, "internal_error", **call(approval()))
        assert cloud.sent == []

    async def test_every_block_is_audited_with_its_reason(self, world):
        gate, _, approval, desk = world
        await blocked(gate, "wrong_target", **call(approval(), target="x"))
        assert desk.audit.find("egress.blocked", "blocked")[-1].detail["reason"] == "wrong_target"


class TestCloudFailure:
    async def test_a_cloud_failure_burns_the_approval_and_is_audited(self, world):
        gate, cloud, approval, desk = world
        a = approval()
        cloud.fail = True
        with pytest.raises(ConnectionError):
            await gate.send(**call(a))
        cloud.fail = False
        await blocked(gate, "already_used", **call(a))  # ask the verifier again; never send twice
        assert desk.audit.find("egress.failed", "error")


class TestContract:
    def test_rejection_needs_a_reason_and_times_must_be_ordered_and_aware(self, desk):
        now = desk.clock.now()
        base = dict(id="appr_0009_zzzz", payload_hash=content_hash(PAYLOAD), target=TARGET,
                    approved_by="safety_verifier", requesting_seat=SEAT, manifest_version="case_briefer@v1",
                    issued_at=now, expires_at=now + timedelta(minutes=1))
        with pytest.raises(ValueError, match="reason"):
            EgressApproval(decision="rejected", **base)
        with pytest.raises(ValueError, match="after"):
            EgressApproval(decision="approved", **{**base, "expires_at": now})
        with pytest.raises(ValueError, match="timezone"):
            EgressApproval(decision="approved", **{**base, "issued_at": now.replace(tzinfo=None)})
        with pytest.raises(ValueError):
            EgressApproval(decision="approved", **{**base, "payload_hash": "abc"})
        with pytest.raises(ValueError):
            EgressApproval(decision="approved", extra_field=1, **base)

    def test_signature_survives_a_json_round_trip(self, desk):
        now = desk.clock.now()
        a = sign_approval(EgressApproval(
            id="appr_0010_yyyy", decision="approved", payload_hash=content_hash(PAYLOAD), target=TARGET,
            approved_by="safety_verifier", requesting_seat=SEAT, manifest_version="case_briefer@v1",
            issued_at=now, expires_at=now + timedelta(minutes=5)), desk.signers["safety_verifier"])
        verify_approval(EgressApproval.model_validate_json(a.model_dump_json()), desk.seat_keys)
        with pytest.raises(SignatureError):
            verify_approval(a.model_copy(update={"id": "appr_0010_xxxx"}), desk.seat_keys)


async def test_single_use_is_atomic_across_replicas_in_postgres(pg):
    uses = PostgresApprovalUses(pg)
    exp = datetime.now(UTC) + timedelta(hours=1)  # the database's clock is the real one, not the test clock
    results = await asyncio.gather(*(asyncio.to_thread(uses.consume, "appr_pg_0001", exp) for _ in range(8)))
    assert results.count(True) == 1
    assert uses.prune(timedelta(0)) == 0  # not expired yet: kept
