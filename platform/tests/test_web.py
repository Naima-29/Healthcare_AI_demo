"""The web desk through its HTTP API: the same calls the frontend makes, on the real seats."""

from __future__ import annotations

import base64
import time
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from kcd.web.app import create_app
from kcd.web.golden import STEPS


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(create_app()) as c:
        yield c


def state(client: TestClient) -> dict:
    return client.get("/api/state").json()


def items(client: TestClient, kind: str) -> list[dict]:
    return [i for i in state(client)["board"] if i["kind"] == kind]


def replies(client: TestClient) -> list[str]:
    return [e["data"]["text"] for e in state(client)["history"] if e["kind"] == "patient_out"]


def events(client: TestClient, kind: str) -> list[dict]:
    return [e["data"] for e in state(client)["history"] if e["kind"] == kind]


def say(client: TestClient, text: str) -> None:
    r = client.post("/api/patient/message", json={"text": text})
    assert r.status_code == 200, r.text


def act(client: TestClient, item: str, action: str, clinician: str, **kw: object):
    return client.post("/api/board/act", json={"item": item, "action": action, "clinician": clinician, **kw})


def read_ct(client: TestClient, study_uid: str | None = None):
    return client.post("/api/pacs/new-study", json={"study_uid": study_uid, "wait": True})


REVIEWER = {"radiologist_review": "Dr. E. Conti", "nephrologist_review": "Dr. A. Rossi",
            "genetic_disclosure": "Dr. P. Greco"}


def review_all(client: TestClient, pack: dict) -> None:
    for gate, review in pack["data"]["reviews"].items():
        if review is None:
            assert act(client, pack["id"], "review", REVIEWER[gate], gate=gate).status_code == 200


def run_golden_path(client: TestClient) -> None:
    for step in STEPS:
        assert client.post("/api/scenario/step", json={"n": step["n"], "wait": True}).status_code == 200


def test_frontend_is_served_and_state_is_complete(client: TestClient) -> None:
    assert "Kidney Care Desk" in client.get("/").text
    assert "boot()" in client.get("/app.js").text
    s = state(client)
    assert s["active"] == "marco-r" and {p["id"] for p in s["patients"]} == {"marco-r", "sofia-b"}
    assert len(s["seats"]) == 8 and s["steps"] == STEPS
    assert s["ct_model"]["mode"] == "stub" and s["patient"]["name"] == "Marco R."


def test_host_header_of_another_site_is_refused(client: TestClient) -> None:
    assert client.get("/api/state", headers={"host": "evil.example"}).status_code == 400


def test_golden_path_end_to_end(client: TestClient) -> None:
    run_golden_path(client)
    s = state(client)
    assert all(e["data"].get("verified") for e in s["history"] if e["kind"] == "a2a")
    types = {e["data"]["type"] for e in s["history"] if e["kind"] == "a2a"}
    assert {"red_flag_escalation", "medication_report", "therapy_conflict", "imaging_signal", "data_delivered",
            "case_pack_ready", "clinician_decision"} <= types
    # The doctor was paged once, and every gate on the Board was closed by a named person's signature.
    pages = events(client, "page")
    assert [p["name"] for p in pages] == ["Dr. A. Rossi"] and "severe flank pain" in pages[0]["message"]
    statuses = {i["kind"]: i["status"] for i in s["board"]}
    assert statuses["escalation"] == "acknowledged" and statuses["imaging"] == "info"
    pack = next(i for i in s["board"] if i["kind"] == "case_pack")
    assert all(r is not None for r in pack["data"]["reviews"].values())
    assert "signed by Dr. A. Rossi" in pack["done_note"]
    assert statuses["case_pack"] == "signed" and statuses["rule"] == "confirmed"
    # The patient heard: the safety script, a hold on the CT, the signed decision, and the confirmed rule.
    said = replies(client)
    assert any("needs medical assessment today" in t for t in said)
    assert any("must be reviewed by your doctor" in t for t in said)
    assert any("Dr. A. Rossi" in t and "reviewed your case" in t and "stop taking ibuprofen" in t for t in said)
    assert "asked you to avoid ibuprofen" in said[-1]
    # Cortex holds it, chained, with the rules confirmed by a person.
    patient, hospital = s["memory"]
    assert patient["chain"]["ok"] and patient["chain"]["length"] > 3
    assert [k["status"] for k in patient["knowledge"]] == ["confirmed"]
    assert [k["status"] for k in hospital["knowledge"]] == ["confirmed"]


def test_step_1_changes_a_manifest_and_reports_an_existing_rule(client: TestClient) -> None:
    client.post("/api/scenario/step", json={"n": 1, "wait": True})
    replies_ = [e["text"] for e in events(client, "director_out")]
    assert "already enforces" in replies_[0]
    assert "Case Briefer is now v2" in replies_[1]
    seat = client.get("/api/seats/case_briefer").json()
    assert [v["version"] for v in seat["versions"]] == [1, 2] and "diagnose" in seat["manifest"]["refuses"]


def test_imaging_question_is_refused_at_the_policy_layer(client: TestClient) -> None:
    say(client, "I had a CT scan yesterday: what did it show?")
    refusal = events(client, "refusal")[-1]
    assert refusal["seat"] == "companion" and refusal["refusal"] == "disclose_imaging_before_review"
    assert "must be reviewed by your doctor" in replies(client)[-1]


def test_genetics_question_is_refused(client: TestClient) -> None:
    say(client, "What does my DNA say?")
    assert events(client, "refusal")[-1]["refusal"] == "disclose_genetics_to_patient"
    assert "specialist" in replies(client)[-1]


def test_case_pack_cannot_be_signed_before_the_radiologist(client: TestClient) -> None:
    assert read_ct(client).json()["sent"] is True
    pack = items(client, "case_pack")[0]
    r = act(client, pack["id"], "sign", "Dr. A. Rossi", items=pack["data"]["draft"])
    assert r.status_code == 409 and "review first: radiologist_review" in r.json()["error"]
    r = act(client, pack["id"], "review", "Dr. A. Rossi", gate="radiologist_review")
    assert r.status_code == 409 and "clinician:radiology" in r.json()["error"]
    assert act(client, pack["id"], "review", "Dr. E. Conti", gate="radiologist_review").status_code == 200
    pack = items(client, "case_pack")[0]
    assert act(client, pack["id"], "sign", "Dr. A. Rossi", items=pack["data"]["draft"]).status_code == 200


def test_edited_wording_is_exactly_what_the_patient_reads(client: TestClient) -> None:
    client.post("/api/scenario/step", json={"n": 2, "wait": True})
    client.post("/api/scenario/step", json={"n": 3, "wait": True})
    pack = items(client, "case_pack")[0]
    review_all(client, pack)
    draft = [{**pack["data"]["draft"][0], "patient_text": "come at 8, Building C"}]
    assert act(client, pack["id"], "sign", "Dr. A. Rossi", items=draft, note="Bring your results.").status_code == 200
    last = replies(client)[-1]
    assert "come at 8, Building C" in last and "Bring your results." in last
    assert pack["data"]["draft"][0]["patient_text"] not in last  # the draft wording was replaced, not added


def test_rejected_rule_is_not_used(client: TestClient) -> None:
    for n in (2, 3, 4, 6):
        client.post("/api/scenario/step", json={"n": n, "wait": True})
    assert act(client, items(client, "rule")[0]["id"], "reject", "Dr. A. Rossi").status_code == 409  # no reason
    for rule in items(client, "rule"):
        assert act(client, rule["id"], "reject", "Dr. A. Rossi", note="not for this patient").status_code == 200
    say(client, "Can I take another ibuprofen tonight? The pain is back.")
    assert "avoid ibuprofen" not in replies(client)[-1]


def test_escalation_moves_up_the_ladder_when_nobody_answers(client: TestClient) -> None:
    client.post("/api/scenario/step", json={"n": 2, "wait": True})
    client.post("/api/scenario/step", json={"n": 3, "wait": True})
    session = client.app.state.session  # type: ignore[attr-defined]
    item = items(client, "escalation")[0]
    assert item["data"]["paged"]["name"] == "Dr. A. Rossi"

    async def overdue() -> None:
        from datetime import timedelta
        session.clock.start += timedelta(minutes=6)
        await session.desk.escalations.tick()

    client.portal.call(overdue)  # type: ignore[union-attr]
    assert items(client, "escalation")[0]["data"]["paged"]["name"] == "Dr. L. Bianchi"
    assert items(client, "escalation")[0]["id"] == item["id"]


def test_sofia_failed_ct_cases_reach_the_board(client: TestClient) -> None:
    assert client.post("/api/patient/select", json={"patient": "sofia-b"}).status_code == 200
    s = state(client)
    assert s["patient"]["name"] == "Sofia B." and len(s["studies"]) == 11
    by_kind = {st["study_uid"].rsplit(".", 1)[1]: st["study_uid"] for st in s["studies"]}

    assert read_ct(client, by_kind["01"]).json()["status"] == "ok"
    assert items(client, "imaging")[0]["data"]["label"] == "normal"
    assert read_ct(client, by_kind["01"]).json()["duplicate"] is True
    assert len(items(client, "imaging")) == 1

    assert read_ct(client, by_kind["05"]).json()["status"] == "out_of_distribution"
    assert read_ct(client, by_kind["08"]).json() == {"status": "unreadable", "sent": False, "duplicate": False}
    assert read_ct(client, by_kind["09"]).json()["status"] == "model_error"
    titles = [i["title"] for i in items(client, "verification_failed")]
    assert any("Safety Verifier" in t for t in titles)
    assert "CT not read: unreadable" in titles and "CT not read: model_error" in titles

    r = read_ct(client, by_kind["11"])
    assert r.status_code == 422 and "CT only" in r.json()["error"]


def test_ocr_error_is_stopped_and_good_photo_reaches_the_pack(client: TestClient) -> None:
    client.post("/api/patient/select", json={"patient": "sofia-b"})
    say(client, "Prendo Brufen 600 tre volte al giorno")
    assert "referto" in replies(client)[-1]
    assert client.post("/api/patient/attach-lab", json={"photo": "ocr-error"}).status_code == 200
    failed = items(client, "verification_failed")
    assert failed and "eGFR plausible" in " ".join(failed[0]["data"]["failed"])
    assert not items(client, "case_pack")
    say(client, "Prendo Brufen 600 tre volte al giorno")
    client.post("/api/patient/attach-lab", json={"photo": "ok"})
    assert items(client, "case_pack")


def test_images_come_from_cortex(client: TestClient) -> None:
    read_ct(client)
    data = items(client, "imaging")[0]["data"]
    for url in (data["key_image"], data["heatmap_ref"]):
        r = client.get(url)
        assert r.status_code == 200 and r.headers["content-type"] == "image/png" and r.content[:4] == b"\x89PNG"
    assert client.get("/api/evidence/marco-r/not-an-id").status_code == 404
    assert client.get("/api/evidence/../secret/x").status_code in (404, 405)


def test_upload_with_the_stub_is_not_given_a_made_up_answer(client: TestClient) -> None:
    png = (client.app.state.session.root / "marco-r" / "ct-2026-10-11" / "key.png").read_bytes()  # type: ignore[attr-defined]
    other = png + b"\0"  # still a valid PNG, but not the study the stub knows
    r = client.post("/api/pacs/upload", json={"name": "../../evil.png", "wait": True,
                                              "data_base64": base64.b64encode(other).decode()})
    assert r.status_code == 200 and r.json()["status"] == "model_error"
    assert not items(client, "imaging")
    assert any(st["description"].endswith("evil.png") for st in state(client)["studies"])
    assert client.post("/api/pacs/upload", json={"name": "x.png", "data_base64": "not base64!"}).status_code == 422


def test_chrislins_model_path_uses_its_own_calibration(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from kcd.imaging.calibration import Calibration
    from kcd.imaging.model import CallableCtModel
    from kcd.web import session as web_session

    version = Calibration.load(web_session.CHRISLIN_CALIBRATION).model_version

    def fake(gray: object) -> dict:
        return {"model_version": version, "labels": ["normal", "cyst", "stone", "tumor"],
                "logits": [0.1, 0.2, 0.3, 3.0]}

    monkeypatch.setattr(web_session, "real_model_available", lambda: True)
    desk = client.app.state.session  # type: ignore[attr-defined]
    desk._real = CallableCtModel(fake)
    assert client.post("/api/pacs/model", json={"mode": "real"}).status_code == 200
    assert state(client)["ct_model"]["mode"] == "real"
    assert read_ct(client).json()["status"] == "ok"
    data = items(client, "imaging")[0]["data"]
    assert data["label"] == "tumor" and data["provenance"]["live_model"] is True and "T=0.798" in data["calibration"]


def test_choosing_chrislins_model_loads_it_in_the_background(client: TestClient,
                                                            monkeypatch: pytest.MonkeyPatch) -> None:
    from kcd.imaging import chrislin_model
    from kcd.web import session as web_session

    loaded = []
    monkeypatch.setattr(web_session, "real_model_available", lambda: True)
    monkeypatch.setattr(chrislin_model, "load", lambda: loaded.append("load"))
    monkeypatch.setattr(chrislin_model, "engine", lambda: "openvino" if loaded else None)
    assert client.post("/api/pacs/model", json={"mode": "real"}).status_code == 200
    for _ in range(50):  # the load runs after the request returns
        if any("is ready" in n["text"] for n in events(client, "note")):
            break
        time.sleep(0.05)
    notes = [n["text"] for n in events(client, "note")]
    assert loaded == ["load"] and any("Loading Chrislin's model" in t for t in notes)
    assert any("ready (engine: openvino)" in t for t in notes)


def test_chrislins_model_is_refused_when_its_weights_are_missing(client: TestClient,
                                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    from kcd.web import session as web_session

    monkeypatch.setattr(web_session, "real_model_available", lambda: False)
    r = client.post("/api/pacs/model", json={"mode": "real"})
    assert r.status_code == 409 and "weights" in r.json()["error"]


def test_erase_removes_the_patient_everywhere(client: TestClient) -> None:
    run_golden_path(client)
    r = client.post("/api/patient/erase", json={})
    assert r.status_code == 200 and r.json()["complete"] is True and r.json()["evidence_rows"] > 0
    s = state(client)
    assert not s["board"] and not s["memory"][0]["evidence"] and not s["memory"][0]["knowledge"]
    assert not replies(client) and not events(client, "a2a")


def test_reset_starts_a_new_desk(client: TestClient) -> None:
    say(client, "What does my DNA say?")
    assert client.post("/api/reset", json={}).status_code == 200
    s = state(client)
    assert not s["history"] and not s["board"] and all(x["metrics"]["refusals"] == 0 for x in s["seats"])


def test_director_doors(client: TestClient) -> None:
    def ask(text: str) -> dict:
        r = client.post("/api/director", json={"text": text})
        assert r.status_code == 200
        return r.json()

    assert "list the agents" in ask("help")["text"]
    assert "Renal Guardian" in ask("List the agents")["text"]
    assert "Renal Guardian v1" in ask("How is the Renal Guardian doing?")["text"]
    created = ask("Create an agent for nephrology phone follow-up: it calls CKD patients on WhatsApp. It must never "
                  "change a therapy; every piece of advice must be approved by the nephrologist.")
    assert "shadow" in created["text"]
    seat = next(s for s in state(client)["seats"] if s["id"] == created["seat"])
    assert seat["shadow"] is True
    assert "did not understand" in ask("Good morning everybody, lovely weather")["text"]


def test_bad_requests_get_a_readable_error(client: TestClient) -> None:
    assert client.post("/api/patient/message", json={"text": " "}).status_code == 422
    assert client.post("/api/patient/message", content="nope").status_code == 400
    r = client.post("/api/board/act", json={"item": "x", "action": "sign", "clinician": "Dr. A. Rossi"})
    assert r.status_code == 404
    assert client.post("/api/patient/select", json={"patient": "nobody"}).status_code == 404
    assert client.post("/api/scenario/step", json={"n": 99}).status_code == 422
