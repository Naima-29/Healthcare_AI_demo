import asyncio

from kcd_core.bus import DISPATCHER_INBOX
from kcd_core.contracts import MessageType
from kcd_core.contracts.messages import MedicationReport, TherapyConflict
from kcd_core.runtime import Seat

from .conftest import payload

CONFLICT = payload(MessageType.THERAPY_CONFLICT)
MEDS = payload(MessageType.MEDICATION_REPORT)


class RenalGuardian(Seat):
    seat_id = "renal_guardian"

    async def handle(self, env, payload, ctx):
        assert isinstance(payload, MedicationReport) and payload.drug_class == "NSAID"
        await ctx.tool("fhir.get_observations", patient=env.patient_id, code="egfr")
        await ctx.send(MessageType.THERAPY_CONFLICT, "safety_verifier", TherapyConflict(**CONFLICT))


class Recorder(Seat):
    def __init__(self, seat_id):
        self.seat_id = seat_id
        self.received = []

    async def handle(self, env, payload, ctx):
        self.received.append((env, payload))


class Rogue(Seat):
    """A Renal Guardian whose model was prompt-injected into messaging the patient."""

    seat_id = "renal_guardian"

    def __init__(self):
        self.fallbacks = []

    async def handle(self, env, payload, ctx):
        await ctx.tool("channel.send", text="Stop your ibuprofen and take 1 g paracetamol.")

    async def fallback(self, env, ctx, error):
        self.fallbacks.append(error)


async def egfr(**_):
    return {"egfr": 52, "date": "2026-10-08"}


def meds(desk):
    return desk.envelope(MessageType.MEDICATION_REPORT, "companion", "renal_guardian", MEDS)


async def test_message_flows_seat_to_dispatcher_to_seat(desk):
    await desk.add_seat(RenalGuardian(), tools={"fhir.get_observations": egfr})
    verifier = Recorder("safety_verifier")
    await desk.add_seat(verifier)

    report = meds(desk)
    await desk.bus.publish(DISPATCHER_INBOX, report)
    await desk.bus.drain()

    [(env, received)] = verifier.received
    assert env.from_ == "renal_guardian" and isinstance(received, TherapyConflict)
    assert env.correlation_id == report.correlation_id and env.causation_id == report.id
    assert env.manifest_version == "renal_guardian@v1"
    assert desk.audit.find("tool.fhir.get_observations")
    assert desk.audit.verify_chain()


async def test_injected_patient_message_is_blocked_and_not_retried(desk):
    rogue = Rogue()
    await desk.add_seat(rogue)
    await desk.bus.publish(DISPATCHER_INBOX, meds(desk))
    await desk.bus.drain()
    assert len(rogue.fallbacks) == 1 and rogue.fallbacks[0].refusal == "send_to_patient"
    assert desk.audit.find("seat.policy_violation", "blocked")
    assert not desk.bus.dead_letters


async def test_refused_action_via_act(desk):
    class Decider(Rogue):
        async def handle(self, env, payload, ctx):
            ctx.act("medication.change_dose")

    decider = Decider()
    await desk.add_seat(decider)
    await desk.bus.publish(DISPATCHER_INBOX, meds(desk))
    await desk.bus.drain()
    assert decider.fallbacks[0].refusal == "change_dose"


async def test_slow_handler_times_out_to_fallback(desk):
    class Slow(Rogue):
        async def handle(self, env, payload, ctx):
            await asyncio.sleep(10)

    slow = Slow()
    await desk.add_seat(slow, timeout_s=0.01)
    await desk.bus.publish(DISPATCHER_INBOX, meds(desk))
    await desk.bus.drain()
    assert len(slow.fallbacks) == 1 and isinstance(slow.fallbacks[0], TimeoutError)


async def test_hot_reload_applies_to_next_message(desk):
    await desk.add_seat(RenalGuardian(), tools={"fhir.get_observations": egfr})
    await desk.add_seat(Recorder("safety_verifier"))
    current = desk.registry.get("renal_guardian")
    desk.registry.activate(current.model_copy(update={
        "version": 2, "tools": [t for t in current.tools if t != "fhir.get_observations"]}))

    await desk.bus.publish(DISPATCHER_INBOX, meds(desk))
    await desk.bus.drain()
    [blocked] = desk.audit.find("pep.tool", "blocked")
    assert blocked.manifest_version == "renal_guardian@v2"

    desk.registry.rollback("renal_guardian")
    assert desk.registry.get("renal_guardian").version == 1


async def test_transient_handler_error_is_retried(desk):
    class Flaky(Recorder):
        async def handle(self, env, payload, ctx):
            if not self.received:
                self.received.append(None)
                raise ConnectionError("FHIR down")
            self.received.append((env, payload))

    flaky = Flaky("safety_verifier")
    await desk.add_seat(flaky)
    await desk.bus.publish(DISPATCHER_INBOX,
                           desk.envelope(MessageType.THERAPY_CONFLICT, "renal_guardian", "safety_verifier", CONFLICT))
    await desk.bus.drain()
    assert len(flaky.received) == 2 and flaky.received[1] is not None


async def test_audit_chain_detects_tampering(desk):
    await desk.bus.publish(DISPATCHER_INBOX,
                           desk.envelope(MessageType.THERAPY_CONFLICT, "renal_guardian", "safety_verifier", CONFLICT))
    await desk.bus.drain()
    assert desk.audit.verify_chain()
    desk.audit.events[0] = desk.audit.events[0].model_copy(update={"decision": "blocked"})
    assert not desk.audit.verify_chain()
