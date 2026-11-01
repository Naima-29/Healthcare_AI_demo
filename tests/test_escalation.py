from datetime import timedelta

from kcd_core.audit import InMemoryAudit
from kcd_core.clock import FakeClock
from kcd_core.contracts import MessageType
from kcd_core.escalation import EscalationService, InMemoryEscalationStore

from .conftest import FakePager, FakeRota
from .test_dispatcher import RED_FLAG


def service(rota=None):
    clock, pager = FakeClock(), FakePager()
    svc = EscalationService(rota=rota or FakeRota(), pager=pager, store=InMemoryEscalationStore(),
                            clock=clock, audit=InMemoryAudit())
    return svc, clock, pager


def targets(pager):
    return [t for t, _ in pager.pages]


async def test_climbs_the_ladder_until_exhausted(desk):
    svc, clock, pager = service()
    env = desk.envelope(MessageType.RED_FLAG_ESCALATION, "companion", "case_briefer", RED_FLAG)
    esc = await svc.open(env, unit="uro_oncology")
    assert targets(pager) == ["dr_rossi"]

    clock.advance(timedelta(minutes=4))
    assert await svc.tick() == []  # still inside the first window

    clock.advance(timedelta(minutes=1))
    await svc.tick()
    assert targets(pager) == ["dr_rossi", "dr_bianchi"]

    clock.advance(timedelta(minutes=5))
    await svc.tick()
    clock.advance(timedelta(minutes=10))
    await svc.tick()
    assert targets(pager) == ["dr_rossi", "dr_bianchi", "dr_verdi", "emergency_desk"]
    assert svc.store.get(esc.id).status == "exhausted"


async def test_ack_stops_the_ladder(desk):
    svc, clock, pager = service()
    env = desk.envelope(MessageType.RED_FLAG_ESCALATION, "companion", "case_briefer", RED_FLAG)
    esc = await svc.open(env, unit="uro_oncology")
    await svc.acknowledge(esc.id, "dr_rossi")
    clock.advance(timedelta(hours=1))
    await svc.tick()
    assert targets(pager) == ["dr_rossi"]
    assert (await svc.acknowledge(esc.id, "dr_bianchi")).acknowledged_by == "dr_rossi"  # idempotent


async def test_empty_rota_slot_escalates_immediately(desk):
    svc, _, pager = service(FakeRota({("uro_oncology", "backup"): "dr_bianchi"}))
    env = desk.envelope(MessageType.RED_FLAG_ESCALATION, "companion", "case_briefer", RED_FLAG)
    await svc.open(env, unit="uro_oncology")
    assert targets(pager) == []
    await svc.tick()  # no primary on call: deadline is now, so the next tick goes straight to backup
    assert targets(pager) == ["dr_bianchi"]


async def test_redelivered_red_flag_pages_once(desk):
    svc, _, pager = service()
    env = desk.envelope(MessageType.RED_FLAG_ESCALATION, "companion", "case_briefer", RED_FLAG)
    first = await svc.open(env, unit="uro_oncology")
    assert (await svc.open(env, unit="uro_oncology")).id == first.id
    assert targets(pager) == ["dr_rossi"]
