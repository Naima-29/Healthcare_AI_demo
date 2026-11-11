import asyncio
import json

import httpx
import pytest
from kcd_core.chief_of_staff import compile_new
from kcd_core.chief_of_staff.diff import manifest_diff
from kcd_core.chief_of_staff.llm import OpenAICompatDraftModel, compile_draft
from kcd_core.contracts import Manifest
from kcd_core.control_plane import (
    Actor,
    Conflict,
    ControlPlane,
    Forbidden,
    InMemoryManifestStore,
    ManifestReloader,
    NotFound,
    PostgresManifestStore,
)
from kcd_core.registry import ManifestRegistry

DIRECTOR = Actor("dir_ferri", frozenset({"director"}))
NURSE = Actor("nurse_x", frozenset({"clinician"}))
AMEND = "Never send the CT report to the patient before the clinician has seen it."
NEW = "Create a seat called Diet Coach that reviews the patient's meal photos against the renal diet plan."


@pytest.fixture
def cp(desk):
    return ControlPlane(registry=desk.registry, store=InMemoryManifestStore(), audit=desk.audit, clock=desk.clock)


class TestDiff:
    def test_flags_widening_and_ignores_noise(self, desk):
        old = desk.registry.get("companion")
        new = old.model_copy(update={"version": 9, "tools": [*old.tools, "fhir.get_observations"],
                                     "surfaces": [*old.surfaces, "fhir"], "refuses": [*old.refuses]})
        d = manifest_diff(old, Manifest.model_validate(new.model_dump()))
        paths = {(c.path, c.kind, c.flag) for c in d.changes}
        assert ("tools", "added", "grants") in paths and d.needs_attention
        assert not any(c.path in ("version", "compiled_at") for c in d.changes)
        assert "[WIDENS]" in d.render()

    def test_removed_refusal_is_flagged_as_loosening(self, desk):
        old = desk.registry.get("companion")
        new = Manifest.model_validate({**old.model_dump(), "refuses": old.refuses[1:]})
        assert any(c.flag == "loosens" for c in manifest_diff(old, new).changes)

    def test_identical_manifest_is_an_empty_diff(self, desk):
        m = desk.registry.get("companion")
        assert manifest_diff(m, m).empty


class TestApproval:
    async def test_only_a_director_may_propose_or_decide(self, cp):
        with pytest.raises(Forbidden):
            await cp.propose(NURSE, AMEND, seat="companion")
        draft, _ = await cp.propose(DIRECTOR, AMEND, seat="companion")
        for fn in (lambda: cp.approve(NURSE, draft.id), lambda: cp.reject(NURSE, draft.id, "no"),
                   lambda: cp.rollback(NURSE, "companion")):
            with pytest.raises(Forbidden):
                fn()

    async def test_propose_changes_nothing_until_approved(self, cp, desk):
        before = desk.registry.get("companion").ref
        draft, _ = await cp.propose(DIRECTOR, AMEND, seat="companion")
        assert draft.status == "pending" and desk.registry.get("companion").ref == before
        assert draft.diff["changes"]  # the Director sees what they approve

    async def test_approve_activates_and_is_audited_and_idempotent(self, cp, desk):
        draft, _ = await cp.propose(DIRECTOR, AMEND, seat="companion")
        done = cp.approve(DIRECTOR, draft.id)
        assert done.status == "approved" and desk.registry.get("companion").version == draft.manifest.version
        cp.approve(DIRECTOR, draft.id)  # double click
        assert len(desk.audit.find("control_plane.activated")) == 1
        assert desk.audit.verify_chain()

    async def test_stale_draft_is_refused(self, cp):
        a, _ = await cp.propose(DIRECTOR, AMEND, seat="companion")
        b, _ = await cp.propose(DIRECTOR, "Always ask the clinician before sharing imaging results.", seat="companion")
        cp.approve(DIRECTOR, a.id)
        with pytest.raises(Conflict, match="changed since"):
            cp.approve(DIRECTOR, b.id)
        assert cp.store.get_draft(b.id).status == "superseded"

    async def test_reject_blocks_later_approval(self, cp, desk):
        before = desk.registry.get("companion").ref
        d, _ = await cp.propose(DIRECTOR, AMEND, seat="companion")
        cp.reject(DIRECTOR, d.id, "too broad")
        with pytest.raises(Conflict):
            cp.approve(DIRECTOR, d.id)
        assert desk.registry.get("companion").ref == before

    async def test_new_seat_starts_with_no_routes_and_cannot_clobber_an_existing_one(self, cp, desk):
        d, _ = await cp.propose(DIRECTOR, NEW)
        m = d.manifest
        assert m.a2a_peers == [] and m.consumes == [] and m.emits == []
        cp.approve(DIRECTOR, d.id)
        assert m.seat in desk.registry.seats()
        with pytest.raises(Conflict, match="already exists"):
            await cp.propose(DIRECTOR, NEW)

    async def test_unknown_seat_and_draft(self, cp):
        with pytest.raises(NotFound):
            await cp.propose(DIRECTOR, AMEND, seat="ghost")
        with pytest.raises(NotFound):
            cp.approve(DIRECTOR, "nope")

    async def test_failed_activation_of_a_new_seat_leaves_no_trace(self, cp, desk):
        d, _ = await cp.propose(DIRECTOR, NEW)
        broken = Manifest.model_validate({**d.manifest.model_dump(), "a2a_peers": ["ghost_seat"]})
        with pytest.raises(Exception):  # noqa: B017
            cp._activate(broken, "dir_ferri", {})
        assert d.manifest.seat not in desk.registry.seats()

    async def test_rollback_restores_content_as_a_new_version(self, cp, desk):
        original = desk.registry.get("companion")
        d, _ = await cp.propose(DIRECTOR, AMEND, seat="companion")
        cp.approve(DIRECTOR, d.id)
        restored = cp.rollback(DIRECTOR, "companion")
        assert restored.version == original.version + 2
        assert restored.refuses == original.refuses and restored.tools == original.tools
        assert desk.registry.get("companion").ref == restored.ref

    async def test_activation_that_breaks_routing_is_rolled_back(self, cp, desk):
        d, _ = await cp.propose(DIRECTOR, AMEND, seat="companion")
        broken = Manifest.model_validate({**d.manifest.model_dump(), "a2a_peers": ["ghost_seat"]})
        live = desk.registry.get("companion").ref
        with pytest.raises(Exception):  # noqa: B017 - RoutingError or ManifestError, both mean "not activated"
            cp._activate(broken, "dir_ferri", {})
        assert desk.registry.get("companion").ref == live


class TestReloader:
    async def test_other_processes_converge_without_restart(self, cp, desk, catalog):
        d, _ = await cp.propose(DIRECTOR, AMEND, seat="companion")
        cp.approve(DIRECTOR, d.id)
        other = ManifestRegistry(catalog)  # a seat container that booted with the old manifests
        for seat in desk.registry.seats():
            other.activate(desk.registry.get(seat))
        other.activate(cp.store.previous("companion"))  # it still runs the version before the change
        reloader = ManifestReloader(other, cp.store)
        assert reloader.sync_once() == [d.manifest.ref]
        assert reloader.sync_once() == []


class TestModelDraft:
    def client(self, content):
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    def model(self, content):
        return OpenAICompatDraftModel("http://llm.test/v1", "m", client=self.client(content))

    async def test_model_cannot_grant_itself_routes_cloud_or_control_plane(self, desk, catalog):
        base = compile_new(NEW, catalog, now=desk.clock.now()).manifest.model_dump()
        evil = {**base, "a2a_peers": ["companion"], "emits": ["red_flag_escalation"], "consumes": ["therapy_conflict"],
                "model": {"edge": "x", "fallback": "cloud-big"}, "memory": {"read": [0, 1, 2, 3], "write": [2, 3]},
                "surfaces": ["control_plane"], "tools": ["control_plane.activate_manifest"]}
        out = await compile_draft(NEW, catalog, now=desk.clock.now(), model=self.model(json.dumps(evil)))
        m = out.manifest
        assert out.used_model
        assert m.a2a_peers == [] and m.emits == [] and m.consumes == [] and m.model.fallback is None
        assert m.memory.write == [] and "control_plane" not in m.surfaces
        assert not any(t.startswith("control_plane.") for t in m.tools)

    async def test_model_cannot_drop_the_safety_floor(self, desk, catalog):
        current = desk.registry.get("companion")
        proposed = {**current.model_dump(), "refuses": [], "gates": ["none"]}
        out = await compile_draft(AMEND, catalog, now=desk.clock.now(), model=self.model(json.dumps(proposed)),
                                  current=current)
        assert out.used_model
        assert set(current.refuses) <= set(out.manifest.refuses)  # every existing refusal survives
        assert set(current.gates) <= set(out.manifest.gates)       # and no gate was dropped

    @pytest.mark.parametrize("content", ["not json at all", '{"seat": 5}', '["list"]'])
    async def test_garbage_from_the_model_falls_back_to_the_rule_draft(self, desk, catalog, content):
        out = await compile_draft(NEW, catalog, now=desk.clock.now(), model=self.model(content))
        assert not out.used_model and out.manifest.seat

    async def test_unreachable_model_falls_back(self, desk, catalog):
        def down(req):
            raise httpx.ConnectError("refused")

        m = OpenAICompatDraftModel("http://x/v1", "m", client=httpx.AsyncClient(transport=httpx.MockTransport(down)))
        out = await compile_draft(NEW, catalog, now=desk.clock.now(), model=m)
        assert not out.used_model and "unavailable" in out.notes[0]

    async def test_slow_model_times_out(self, desk, catalog):
        class Slow:
            async def draft(self, **kw):
                await asyncio.sleep(5)

        out = await compile_draft(NEW, catalog, now=desk.clock.now(), model=Slow(), timeout_s=0.05)
        assert not out.used_model


async def test_postgres_store_round_trip(pg, desk):
    store = PostgresManifestStore(pg)
    cp = ControlPlane(registry=desk.registry, store=store, audit=desk.audit, clock=desk.clock)
    boot = desk.registry.get("companion")
    d, _ = await cp.propose(DIRECTOR, AMEND, seat="companion")
    assert store.get_draft(d.id).manifest == d.manifest and store.list_drafts("pending")[0].id == d.id
    cp.approve(DIRECTOR, d.id)
    assert store.active("companion").version == d.manifest.version
    assert store.previous("companion").version == boot.version  # the boot manifest was seeded as history
    restored = cp.rollback(DIRECTOR, "companion")
    assert store.active("companion").version == restored.version
    with pg.connection() as conn:
        assert conn.execute("SELECT count(*) FROM manifest WHERE seat='companion' AND active").fetchone()[0] == 1
