"""MCP connectors over a real MCP session (in-memory transport), with the upstream systems faked."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
from kcd_core.connectors.bridge import ToolCallError, bind_session
from kcd_core.connectors.fhir import FhirClient, build_fhir_server
from kcd_core.connectors.oncall import build_oncall_server
from kcd_core.contracts import MessageType
from kcd_core.pagers import LogPager, PagerError
from kcd_core.runtime import Seat, ToolRegistry
from mcp.shared.memory import create_connected_server_and_client_session as connect

from .conftest import FakeRota, payload

OBS = {"resourceType": "Bundle", "entry": [{"resource": {
    "id": "o1", "status": "final", "code": {"coding": [{"system": "http://loinc.org", "code": "98979-8",
                                                          "display": "eGFR"}]},
    "valueQuantity": {"value": 38, "unit": "mL/min/1.73m2"}, "effectiveDateTime": "2026-10-30T08:00:00Z"}}]}


class Upstream:
    """A fake FHIR server that records what it was asked."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.status = 200

    def handler(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        if self.status != 200:
            return httpx.Response(self.status, text="secret internal stack trace with patient name Mario Rossi")
        path = req.url.path
        if path.endswith("/Patient/pt_1"):
            return httpx.Response(200, json={"id": "pt_1", "birthDate": "1957-03-02", "gender": "male",
                                             "name": [{"family": "Rossi"}], "address": [{"city": "Roma"}]})
        if path.endswith("/Observation"):
            return httpx.Response(200, json=OBS)
        return httpx.Response(200, json={"resourceType": "Bundle", "entry": []})


def fhir_server(up: Upstream):
    client = httpx.AsyncClient(base_url="http://fhir.test/fhir/", transport=httpx.MockTransport(up.handler))
    return build_fhir_server(FhirClient("http://fhir.test/fhir", client=client))


async def call(server, tool, **args):
    async with connect(server._mcp_server) as session:
        return await session.call_tool(tool, args)


class TestFhir:
    async def test_tools_match_the_catalogue(self, catalog):
        async with connect(fhir_server(Upstream())._mcp_server) as s:
            names = {t.name for t in (await s.list_tools()).tools}
        assert names == set(catalog.surfaces["fhir"])

    async def test_observations_are_compact_and_use_the_seats_call_shape(self):
        up = Upstream()
        reg = ToolRegistry()
        async with connect(fhir_server(up)._mcp_server) as session:
            await bind_session(reg, session)
            rows = await reg.get("fhir.get_observations")(patient="pt_1", code="eGFR")  # exactly as seats call it
        assert rows == [{"id": "o1", "code": {"system": "http://loinc.org", "code": "98979-8", "display": "eGFR"},
                         "value": 38, "unit": "mL/min/1.73m2", "effective": "2026-10-30T08:00:00Z", "status": "final"}]
        q = up.requests[0].url.params
        assert q["patient"] == "pt_1" and "http://loinc.org|98979-8" in q["code"] and q["_sort"] == "-date"

    async def test_patient_demographics_leave_out_name_and_address(self):
        res = await call(fhir_server(Upstream()), "fhir.get_patient", patient="pt_1")
        text = res.content[0].text
        assert "1957" in text and "Rossi" not in text and "Roma" not in text

    @pytest.mark.parametrize("bad", ["../Patient", "pt 1", "pt_1?x=1", "a" * 80, "", "pt/1", "pt_1&_include=*"])
    async def test_identifiers_cannot_inject_into_the_upstream_url(self, bad):
        up = Upstream()
        res = await call(fhir_server(up), "fhir.get_observations", patient=bad)
        assert res.isError and up.requests == []  # rejected before any request was made

    async def test_unknown_code_and_bad_date_are_rejected(self):
        up = Upstream()
        assert (await call(fhir_server(up), "fhir.get_observations", patient="pt_1", code="drop table")).isError
        assert (await call(fhir_server(up), "fhir.get_observations", patient="pt_1", since="yesterday")).isError
        assert up.requests == []

    async def test_upstream_failure_does_not_leak_its_body(self):
        up = Upstream()
        up.status = 500
        res = await call(fhir_server(up), "fhir.get_observations", patient="pt_1")
        text = res.content[0].text
        assert res.isError and "unavailable" in text and "Mario" not in text and "stack trace" not in text

    async def test_not_found_is_a_clean_error(self):
        up = Upstream()
        up.status = 404
        assert "not found" in (await call(fhir_server(up), "fhir.get_patient", patient="pt_x")).content[0].text


class TestOnCall:
    def server(self, pager=None):
        return build_oncall_server(FakeRota(), pager or LogPager())

    async def test_ladder_and_single_role(self, catalog):
        async with connect(self.server()._mcp_server) as s:
            assert {t.name for t in (await s.list_tools()).tools} == set(catalog.surfaces["oncall"])
        reg = ToolRegistry()
        async with connect(self.server()._mcp_server) as session:
            await bind_session(reg, session)
            out = await reg.get("oncall.who_is_on_call")(role="backup")
        assert out["clinician_id"] == "dr_bianchi"
        assert [r["clinician_id"] for r in out["ladder"]] == ["dr_rossi", "dr_bianchi", "dr_verdi"]

    async def test_page_sends_the_message_to_a_named_clinician_only(self):
        sent = []

        class Rec(LogPager):
            async def page_message(self, target, message, *, unit, patient_id=None):
                sent.append((target, message, unit, patient_id))
                return "pg_1"

        reg = ToolRegistry()
        async with connect(self.server(Rec())._mcp_server) as session:
            await bind_session(reg, session)
            out = await reg.get("oncall.page")(name="dr_rossi", message="RED FLAG pt_1", patient="pt_1",
                                               pager="+39 333 0000000")  # a number is accepted but never used
        assert out == {"paged": "dr_rossi", "page_id": "pg_1"}
        assert sent == [("dr_rossi", "RED FLAG pt_1", "uro_oncology", "pt_1")]

    async def test_a_failed_page_is_an_error_not_a_silent_success(self):
        class Down(LogPager):
            async def page_message(self, *a, **k):
                raise PagerError("gateway down")

        reg = ToolRegistry()
        async with connect(self.server(Down())._mcp_server) as session:
            await bind_session(reg, session)
            with pytest.raises(ToolCallError):
                await reg.get("oncall.page")(name="dr_rossi", message="x")

    @pytest.mark.parametrize("args", [{"name": "dr rossi", "message": "x"}, {"name": "dr_rossi", "message": " "},
                                      {"name": "dr_rossi", "message": "x" * 501}])
    async def test_page_input_is_validated(self, args):
        assert (await call(self.server(), "oncall.page", **args)).isError

    async def test_bad_role_and_time(self):
        assert (await call(self.server(), "oncall.who_is_on_call", role="ceo")).isError
        assert (await call(self.server(), "oncall.who_is_on_call", at="soon")).isError
        ok = await call(self.server(), "oncall.who_is_on_call", at=datetime(2026, 11, 10, 9, tzinfo=UTC).isoformat())
        assert not ok.isError


async def test_a_seat_reaches_an_mcp_server_only_through_the_pep(desk):
    """Seat -> PEP (manifest allows it, audited) -> bridge -> MCP -> upstream."""
    up = Upstream()
    registry = ToolRegistry()
    async with connect(fhir_server(up)._mcp_server) as session:
        await bind_session(registry, session)
        seen = {}

        class Guardian(Seat):
            seat_id = "renal_guardian"

            async def handle(self, env, payload_, ctx):
                seen["egfr"] = await ctx.tool("fhir.get_observations", patient=env.patient_id, code="eGFR")

        rt = await desk.add_seat(Guardian(), tools=registry._tools)
        env = desk.envelope(MessageType.MEDICATION_REPORT, "companion", "renal_guardian",
                            payload(MessageType.MEDICATION_REPORT))
        await rt._on_message(env)
    assert seen["egfr"][0]["value"] == 38
    assert desk.audit.find("tool.fhir.get_observations") and desk.audit.verify_chain()
