from kcd_core.bus import DISPATCHER_INBOX, seat_inbox
from kcd_core.contracts import MessageType

from .conftest import sha

CONFLICT = {
    "drug": "ibuprofen", "condition": "CKD stage 3b", "rule_id": "DD-NSAID-CKD", "severity": "contraindicated",
    "explanation": "NSAIDs reduce renal perfusion.", "citations": [{"source": "KDIGO 2024", "ref": "3.4.1"}],
}
RED_FLAG = {
    "flags": [{"code": "gross_hematuria", "detected_by": "rules", "evidence": "blood in urine"}],
    "severity": "critical", "unit": "nephrology", "safety_script_sent": True,
}


def delivered_to(desk, seat):
    return [e for s, e in desk.bus.published if s == seat_inbox(seat)]


async def test_routes_valid_message(desk):
    env = desk.envelope(MessageType.THERAPY_CONFLICT, "renal_guardian", "safety_verifier", CONFLICT)
    await desk.bus.publish(DISPATCHER_INBOX, env)
    await desk.bus.drain()
    assert delivered_to(desk, "safety_verifier") == [env]
    assert desk.audit.find("dispatch.routed")


async def test_unsigned_message_is_dead_lettered(desk):
    env = desk.envelope(MessageType.THERAPY_CONFLICT, "renal_guardian", "safety_verifier", CONFLICT, sign=False)
    await desk.bus.publish(DISPATCHER_INBOX, env)
    await desk.bus.drain()
    assert not delivered_to(desk, "safety_verifier")
    assert len(desk.bus.dead_letters) == 1 and desk.bus.dead_letters[0].attempts == 1


async def test_route_not_in_manifests_is_rejected(desk):
    # Companion may not address the Imaging Reader, and the Imaging Reader does not consume symptom reports.
    payload = {"symptoms": [{"code": "x", "label": "x", "severity": 1}], "language": "en", "channel": "telegram"}
    env = desk.envelope(MessageType.SYMPTOM_REPORT, "companion", "imaging_reader", payload)
    await desk.bus.publish(DISPATCHER_INBOX, env)
    await desk.bus.drain()
    assert not delivered_to(desk, "imaging_reader")
    assert "may not address" in desk.bus.dead_letters[0].error


async def test_skipping_the_safety_verifier_is_rejected(desk):
    env = desk.envelope(MessageType.THERAPY_CONFLICT, "renal_guardian", "case_briefer", CONFLICT)
    await desk.bus.publish(DISPATCHER_INBOX, env)
    await desk.bus.drain()
    assert not delivered_to(desk, "case_briefer")
    assert desk.bus.dead_letters


async def test_duplicate_is_delivered_once(desk):
    env = desk.envelope(MessageType.THERAPY_CONFLICT, "renal_guardian", "safety_verifier", CONFLICT)
    await desk.bus.publish(DISPATCHER_INBOX, env)
    await desk.bus.publish(DISPATCHER_INBOX, env)
    await desk.bus.drain()
    assert len(delivered_to(desk, "safety_verifier")) == 1
    assert desk.audit.find("dispatch.duplicate")


async def test_red_flag_pages_on_call_and_routes_to_case_briefer(desk):
    env = desk.envelope(MessageType.RED_FLAG_ESCALATION, "companion", "case_briefer", RED_FLAG)
    await desk.bus.publish(DISPATCHER_INBOX, env)
    await desk.bus.drain()
    assert [t for t, _ in desk.pager.pages] == ["dr_rossi"]
    assert delivered_to(desk, "case_briefer") == [env]


async def test_case_pack_opens_gate(desk):
    pack = {"artifact_id": "cp_1", "artifact_hash": sha("pack"), "gate_role": "clinician:nephrology",
            "summary": "NSAID in CKD 3b"}
    env = desk.envelope(MessageType.CASE_PACK_READY, "case_briefer", "dispatcher", pack)
    await desk.bus.publish(DISPATCHER_INBOX, env)
    await desk.bus.drain()
    assert [g.artifact_id for g in desk.gates.opened] == ["cp_1"]


async def test_delivery_failure_is_retried_not_dropped(desk):
    env = desk.envelope(MessageType.THERAPY_CONFLICT, "renal_guardian", "safety_verifier", CONFLICT)
    real_publish = desk.bus.publish
    failures = {"left": 1}

    async def flaky_publish(subject, e):
        if subject == seat_inbox("safety_verifier") and failures["left"]:
            failures["left"] -= 1
            raise ConnectionError("bus hiccup")
        await real_publish(subject, e)

    desk.bus.publish = flaky_publish
    await real_publish(DISPATCHER_INBOX, env)
    await desk.bus.drain()
    assert delivered_to(desk, "safety_verifier") == [env]
    assert not desk.bus.dead_letters
