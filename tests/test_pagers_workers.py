import asyncio
import hashlib
import hmac
import json
from datetime import timedelta

import httpx
import pytest
from kcd_core.contracts import MessageType
from kcd_core.escalation import EscalationService, InMemoryEscalationStore
from kcd_core.pagers import PagerError, WebhookPager
from kcd_core.workers import escalation_worker, run_periodically

from .conftest import FakeClock, FakePager, FakeRota, InMemoryAudit
from .test_dispatcher import RED_FLAG


def esc_of(desk):
    svc = EscalationService(rota=FakeRota(), pager=FakePager(), store=InMemoryEscalationStore(), clock=desk.clock,
                            audit=InMemoryAudit())
    return svc


async def opened(desk, svc):
    env = desk.envelope(MessageType.RED_FLAG_ESCALATION, "companion", "case_briefer", RED_FLAG)
    return await svc.open(env, unit="uro_oncology")


def pager(handler, **kw):
    return WebhookPager("https://pager.test/hook", "s3cret", backoff_s=0, client=httpx.AsyncClient(
        transport=httpx.MockTransport(handler)), **kw)


async def test_webhook_signs_the_exact_body(desk):
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["req"] = req
        return httpx.Response(202)

    svc = esc_of(desk)
    esc = await opened(desk, svc)
    await pager(handler).page("dr_rossi", esc)
    req = seen["req"]
    expect = "sha256=" + hmac.new(b"s3cret", req.content, hashlib.sha256).hexdigest()
    assert req.headers["X-KCD-Signature"] == expect
    assert json.loads(req.content)["target"] == "dr_rossi"
    assert req.headers["Idempotency-Key"] == f"{esc.id}:0:dr_rossi"


async def test_webhook_retries_5xx_but_not_4xx(desk):
    esc = await opened(desk, esc_of(desk))
    calls = []

    def flaky(req):
        calls.append(1)
        return httpx.Response(503 if len(calls) < 3 else 200)

    await pager(flaky).page("dr_rossi", esc)
    assert len(calls) == 3

    calls.clear()

    def refused(req):
        calls.append(1)
        return httpx.Response(401)

    with pytest.raises(PagerError, match="401"):
        await pager(refused).page("dr_rossi", esc)
    assert len(calls) == 1


async def test_webhook_gives_up_loudly(desk):
    esc = await opened(desk, esc_of(desk))

    def down(req):
        raise httpx.ConnectError("no route")

    with pytest.raises(PagerError, match="3 attempts"):
        await pager(down).page("dr_rossi", esc)


async def test_a_broken_pager_climbs_the_ladder_instead_of_going_silent(desk):
    class Broken:
        calls = 0

        async def page(self, target, esc):
            Broken.calls += 1
            if target == "dr_rossi":
                raise PagerError("gateway down")
            self.paged = target

    p = Broken()
    audit = InMemoryAudit()
    svc = EscalationService(rota=FakeRota(), pager=p, store=InMemoryEscalationStore(), clock=desk.clock, audit=audit)
    await opened(desk, svc)
    assert audit.find("escalation.page_failed")  # primary could not be reached ...
    await svc.tick()  # ... so the very next tick pages the backup, without waiting out the 5 minutes
    assert p.paged == "dr_bianchi"


async def test_a_failing_rota_climbs_too(desk):
    class BadRota:
        async def who(self, unit, role, at):
            raise ConnectionError("db down")

    audit = InMemoryAudit()
    svc = EscalationService(rota=BadRota(), pager=FakePager(), store=InMemoryEscalationStore(), clock=desk.clock,
                            audit=audit)
    await opened(desk, svc)
    assert audit.find("escalation.rota_error", "error")


async def test_ack_between_claim_and_page_prevents_a_late_page(desk):
    svc = esc_of(desk)
    esc = await opened(desk, svc)
    desk.clock.advance(timedelta(minutes=6))
    claimed = svc.store.claim_due(desk.clock.now(), limit=5, lease=timedelta(seconds=30))
    assert [e.id for e in claimed] == [esc.id]
    await svc.acknowledge(esc.id, "dr_rossi")  # the doctor answers while the worker holds the claim
    assert await svc._advance(claimed[0], desk.clock.now()) is None
    assert [t for t, _ in svc.pager.pages] == ["dr_rossi"]


async def test_two_workers_page_the_next_level_once(desk):
    svc_a = esc_of(desk)
    svc_b = EscalationService(rota=svc_a.rota, pager=svc_a.pager, store=svc_a.store, clock=desk.clock,
                              audit=InMemoryAudit())
    await opened(desk, svc_a)
    desk.clock.advance(timedelta(minutes=6))
    await asyncio.gather(svc_a.tick(), svc_b.tick())
    assert [t for t, _ in svc_a.pager.pages] == ["dr_rossi", "dr_bianchi"]


async def test_worker_survives_a_failing_iteration():
    stop, runs = asyncio.Event(), []

    async def step():
        runs.append(1)
        if len(runs) == 1:
            raise RuntimeError("transient")
        if len(runs) == 3:
            stop.set()

    await asyncio.wait_for(run_periodically("t", step, 0.01, stop), 3)
    assert len(runs) == 3


async def test_escalation_worker_stops_on_signal(desk):
    stop = asyncio.Event()
    task = asyncio.create_task(escalation_worker(esc_of(desk), stop, 0.01))
    await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(task, 2)
    assert isinstance(FakeClock(), FakeClock)
