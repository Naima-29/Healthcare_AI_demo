"""Postgres adapters against a real database with the real migrations (skipped when Postgres is unreachable)."""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta, timezone

import psycopg
import pytest
from kcd_core.adapters.pg_stores import (
    PostgresAuditSink,
    PostgresEscalationStore,
    PostgresGateQueue,
    PostgresIdempotencyStore,
    PostgresOnCallRota,
)
from kcd_core.contracts import MessageType
from kcd_core.contracts.messages import CasePackReady
from kcd_core.escalation import Escalation, EscalationConflict

from .conftest import FakeClock, payload

NOW = datetime(2026, 11, 10, 9, 0, tzinfo=UTC)


class TestIdempotency:
    def test_mark_and_has(self, pg):
        store = PostgresIdempotencyStore(pg)
        assert store.has("k1") is False
        store.mark("k1")
        store.mark("k1")  # idempotent
        assert store.has("k1") is True

    def test_prune_drops_only_old_keys(self, pg):
        store = PostgresIdempotencyStore(pg)
        store.mark("old")
        with pg.connection() as conn:
            conn.execute("UPDATE idempotency SET marked_at = now() - interval '2 days' WHERE key = 'old'")
        store.mark("new")
        assert store.prune(timedelta(days=1)) == 1
        assert store.has("old") is False and store.has("new") is True


class TestAudit:
    def test_chain_links_and_verifies(self, pg):
        sink = PostgresAuditSink(pg)
        e1 = sink.record(ts=NOW, actor="companion", action="tool.fhir.get_patient", decision="info")
        e2 = sink.record(ts=NOW, actor="companion", action="seat.handled", decision="info", detail={"n": 1.5})
        assert e2.prev_hash == e1.hash
        assert sink.verify_chain() is True

    def test_find_filters(self, pg):
        sink = PostgresAuditSink(pg)
        sink.record(ts=NOW, actor="a", action="dispatch.routed", decision="allowed")
        sink.record(ts=NOW, actor="a", action="dispatch.reject", decision="blocked")
        sink.record(ts=NOW, actor="a", action="dispatch.routed", decision="allowed")
        assert len(sink.find("dispatch.routed")) == 2
        assert len(sink.find("dispatch.reject", "blocked")) == 1

    def test_chain_survives_a_non_utc_timestamp(self, pg):
        # The hash covers the serialised timestamp; a +05:30 caller must not break verification after the DB
        # hands the value back as UTC.
        sink = PostgresAuditSink(pg)
        sink.record(ts=datetime(2026, 11, 10, 14, 30, tzinfo=timezone(timedelta(hours=5, minutes=30))),
                    actor="a", action="x", decision="info")
        assert sink.verify_chain() is True

    def test_naive_timestamp_is_refused(self, pg):
        with pytest.raises(ValueError, match="timezone-aware"):
            PostgresAuditSink(pg).record(ts=datetime(2026, 11, 10), actor="a", action="x", decision="info")

    def test_nul_characters_do_not_poison_the_log(self, pg):
        sink = PostgresAuditSink(pg)
        sink.record(ts=NOW, actor="a", action="dispatch.reject", decision="blocked", detail={"error": "bad\x00input"})
        assert sink.verify_chain() is True

    def test_database_rejects_edits_and_deletes(self, pg):
        sink = PostgresAuditSink(pg)
        sink.record(ts=NOW, actor="a", action="x", decision="info")
        for stmt in ("UPDATE audit_event SET actor = 'evil'", "DELETE FROM audit_event", "TRUNCATE audit_event"):
            with pytest.raises(psycopg.errors.IntegrityConstraintViolation), pg.connection() as conn:
                conn.execute(stmt)

    def test_tampering_behind_the_triggers_is_detected(self, pg):
        sink = PostgresAuditSink(pg)
        for i in range(3):
            sink.record(ts=NOW, actor="a", action=f"x{i}", decision="info")
        with pg.connection() as conn:  # an owner who disables the trigger still cannot hide the edit
            conn.execute("ALTER TABLE audit_event DISABLE TRIGGER audit_event_no_change")
            conn.execute("UPDATE audit_event SET actor = 'evil' WHERE action = 'x1'")
        assert sink.verify_chain() is False

    def test_concurrent_writers_cannot_fork_the_chain(self, pg):
        sink = PostgresAuditSink(pg)
        errors: list[BaseException] = []

        def writer(n: int) -> None:
            try:
                for i in range(10):
                    sink.record(ts=NOW, actor=f"w{n}", action=f"a{i}", decision="info")
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=writer, args=(n,)) for n in range(6)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        assert not errors
        assert sink.verify_chain() is True
        with pg.connection() as conn:
            assert conn.execute("SELECT count(*) FROM audit_event").fetchone()[0] == 60


def _esc(**kw: object) -> Escalation:
    base: dict[str, object] = dict(id="e1", message_id="m1", patient_id="pt", episode_id="ep", unit="uro",
                                   level=0, deadline=NOW, status="open", paged=(), acknowledged_by=None)
    base.update(kw)
    return Escalation(**base)  # type: ignore[arg-type]


class TestEscalationStore:
    def test_round_trip_and_upsert(self, pg):
        store = PostgresEscalationStore(pg)
        assert store.put(_esc(paged=("dr_a",))) is True
        assert store.put(_esc(level=1, paged=("dr_a", "dr_b"))) is True
        got = store.get("e1")
        assert got is not None and got.level == 1 and got.paged == ("dr_a", "dr_b") and got.deadline == NOW

    def test_acknowledged_row_is_never_overwritten(self, pg):
        store = PostgresEscalationStore(pg)
        store.put(_esc())
        assert store.put(_esc(status="acknowledged", acknowledged_by="dr_a")) is True
        # a late tick tries to move the same escalation up a level
        assert store.put(_esc(level=1, status="open")) is False
        got = store.get("e1")
        assert got is not None and got.status == "acknowledged" and got.level == 0

    def test_one_escalation_per_message(self, pg):
        store = PostgresEscalationStore(pg)
        store.put(_esc(id="e1", message_id="m1"))
        with pytest.raises(EscalationConflict):
            store.put(_esc(id="e2", message_id="m1"))

    def test_due_and_for_message(self, pg):
        store = PostgresEscalationStore(pg)
        store.put(_esc(id="e1", message_id="m1", deadline=NOW - timedelta(minutes=1)))
        store.put(_esc(id="e2", message_id="m2", deadline=NOW + timedelta(minutes=10)))
        store.put(_esc(id="e3", message_id="m3", deadline=NOW - timedelta(minutes=5), status="acknowledged"))
        assert [e.id for e in store.due(NOW)] == ["e1"]
        assert store.for_message("m2") is not None and store.for_message("nope") is None

    def test_claims_are_exclusive_and_expire(self, pg):
        store = PostgresEscalationStore(pg)
        for i in range(5):
            store.put(_esc(id=f"e{i}", message_id=f"m{i}", deadline=NOW - timedelta(minutes=i + 1)))
        first = store.claim_due(NOW, limit=3, lease=timedelta(seconds=30))
        second = store.claim_due(NOW, limit=10, lease=timedelta(seconds=30))
        assert len(first) == 3 and len(second) == 2
        assert not {e.id for e in first} & {e.id for e in second}  # nobody is handed the same row twice
        assert store.claim_due(NOW, limit=10, lease=timedelta(seconds=30)) == []
        # the worker that took them crashed: after the lease they are claimable again
        assert len(store.claim_due(NOW + timedelta(seconds=31), limit=10, lease=timedelta(seconds=30))) == 5

    def test_parallel_claimers_get_disjoint_rows(self, pg):
        store = PostgresEscalationStore(pg)
        for i in range(40):
            store.put(_esc(id=f"e{i}", message_id=f"m{i}", deadline=NOW - timedelta(seconds=i)))
        got: list[list[str]] = []

        def claim() -> None:
            got.append([e.id for e in store.claim_due(NOW, limit=40, lease=timedelta(minutes=1))])

        threads = [threading.Thread(target=claim) for _ in range(6)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        flat = [i for ids in got for i in ids]
        assert len(flat) == 40 and len(set(flat)) == 40


class TestRotaAndGates:
    async def test_rota_picks_the_shift_that_covers_the_moment(self, pg):
        rota = PostgresOnCallRota(pg)
        rota.add_shift("uro", "primary", "dr_rossi", NOW - timedelta(hours=8), NOW + timedelta(hours=4))
        rota.add_shift("uro", "primary", "dr_neri", NOW + timedelta(hours=4), NOW + timedelta(hours=12))
        assert await rota.who("uro", "primary", NOW) == "dr_rossi"
        assert await rota.who("uro", "primary", NOW + timedelta(hours=5)) == "dr_neri"
        assert await rota.who("uro", "backup", NOW) is None
        assert await rota.who("uro", "primary", NOW + timedelta(days=2)) is None

    async def test_gate_open_is_idempotent_on_case_and_version(self, pg, desk):
        gates = PostgresGateQueue(pg)
        pack = CasePackReady.model_validate(payload(MessageType.CASE_PACK_READY))
        env = desk.envelope(
            MessageType.CASE_PACK_READY, "case_briefer", "dispatcher", payload(MessageType.CASE_PACK_READY)
        )
        await gates.open_gate(env, pack)
        await gates.open_gate(env, pack)  # a bus retry
        pending = gates.pending()
        assert len(pending) == 1 and pending[0]["case_id"] == pack.case_id


def test_fake_clock_is_not_used_by_accident():
    assert FakeClock().now().tzinfo is not None
