"""The web desk through its HTTP API: the calls Chrislin's frontend makes, on the real seats."""

from __future__ import annotations

import base64
import time
from collections.abc import Iterator
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from kcd.web.app import create_app
from kcd.web.golden import STEPS

MARCO, SOFIA = "marco-r", "sofia-b"
REVIEWER = {"radiologist_review": "dr_conti", "nephrologist_review": "dr_rossi", "genetic_disclosure": "dr_greco"}


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(create_app()) as c:
        yield c


def state(client: TestClient) -> dict:
    return client.get("/api/state").json()


def items(client: TestClient, kind: str, patient: str = MARCO, status: str | None = None) -> list[dict]:
    return [i for i in state(client)["board"] if i["kind"] == kind and i["patient_id"] == patient
            and (status is None or i["status"] == status)]


def replies(client: TestClient, patient: str = MARCO) -> list[str]:
    return [e["data"]["text"] for e in state(client)["history"]
            if e["kind"] == "patient_out" and e["data"]["patient"] == patient]


def events(client: TestClient, kind: str) -> list[dict]:
    return [e["data"] for e in state(client)["history"] if e["kind"] == kind]


def say(client: TestClient, text: str, patient: str = MARCO) -> None:
    r = client.post("/api/patient/message", json={"text": text, "patient": patient})
    assert r.status_code == 200, r.text


def act(client: TestClient, item: str, action: str, clinician: str, **kw: object):
    return client.post("/api/board/act", json={"item": item, "action": action, "clinician": clinician, **kw})


def step(client: TestClient, n: int, patient: str = MARCO) -> None:
    r = client.post("/api/scenario/step", json={"n": n, "patient": patient, "wait": True})
    assert r.status_code == 200, r.text


def read_ct(client: TestClient, study_uid: str | None = None):
    return client.post("/api/pacs/new-study", json={"study_uid": study_uid, "wait": True})


def open_pack(client: TestClient, patient: str = MARCO) -> dict:
    """The open case pack; like Chrislin's desk, it goes on the Board at golden path step 5."""
    if not items(client, "case_pack", patient, "open"):
        step(client, 5, patient)
    return items(client, "case_pack", patient, "open")[0]


def signals(client: TestClient, patient: str = MARCO) -> list[dict]:
    """Verified CT signals: what the Safety Verifier passed on to the Case Briefer."""
    return [e["payload"] for e in events(client, "a2a")
            if e["type"] == "imaging_signal" and e["to"] == "case_briefer" and e["patient"] == patient]


def review_all(client: TestClient, pack: dict) -> None:
    for gate, review in pack["reviews"].items():
        if review is None:
            assert act(client, pack["id"], "review", REVIEWER[gate], gate=gate).status_code == 200


def sign(client: TestClient, patient: str = MARCO, **kw: object) -> dict:
    pack = open_pack(client, patient)
    review_all(client, pack)
    items_ = kw.pop("items", [{"code": d["code"], "selected": True, "patient_text": d["patient_text"]}
                              for d in pack["data"]["draft"]])
    r = act(client, pack["id"], "sign", "dr_rossi", items=items_, **kw)
    assert r.status_code == 200, r.text
    return pack


def test_chrislins_frontend_is_served_with_the_state_it_reads(client: TestClient) -> None:
    page = client.get("/").text
    assert "Kidney Care Desk" in page and "/app.css" in page and "boot()" in client.get("/app.js").text
    assert client.get("/app.css").status_code == 200 and client.get("/assets/loopnexus.png").status_code == 200
    s = state(client)
    assert s["patient"] == MARCO and [p["id"] for p in s["patients"]] == [MARCO, SOFIA]
    assert all(p["initials"] and p["age"] > 0 and p["script"] and p["ibuprofen"] for p in s["patients"])
    assert s["steps"] == STEPS and len(STEPS) == 5 and s["light"] is False
    assert all(c["role"].startswith("clinician:") and c["id"] for c in s["clinicians"])
    assert s["integrity"] == {"audit_chain_ok": True, "dead_letters": 0} and len(s["seats"]) == 8


def test_host_header_of_another_site_is_refused(client: TestClient) -> None:
    assert client.get("/api/state", headers={"host": "evil.example"}).status_code == 400


def test_golden_path_for_marco_then_the_clinicians_sign(client: TestClient) -> None:
    for s in STEPS:
        step(client, s["n"])
    # The red flag paged the on-call doctor once; the uro-oncologist acknowledges it.
    pages = events(client, "page")
    assert [p["name"] for p in pages] == ["Dr. A. Rossi"] and "severe flank pain" in pages[0]["message"]
    esc = items(client, "escalation", status="open")[0]
    assert esc["data"]["paged"] == ["Dr. A. Rossi"] and esc["data"]["unit"]
    assert act(client, esc["id"], "acknowledge", "dr_bianchi").status_code == 200
    # One open case pack (older versions superseded), with all three specialist gates.
    assert len(items(client, "case_pack", status="open")) == 1
    pack = open_pack(client)
    assert set(pack["reviews"]) == {"radiologist_review", "nephrologist_review", "genetic_disclosure"}
    assert pack["data"]["images"]["key_image"].startswith("cortex:evidence/")
    sign(client)
    rules = items(client, "rule", status="open")
    assert len(rules) == 2
    for rule in rules:
        assert act(client, rule["id"], "confirm", "dr_rossi").status_code == 200

    s = state(client)
    types = {e["data"]["type"] for e in s["history"] if e["kind"] == "a2a"}
    assert {"red_flag_escalation", "medication_report", "therapy_conflict", "imaging_signal", "data_delivered",
            "case_pack_ready", "clinician_decision"} <= types
    said = replies(client)
    assert any("needs medical assessment today" in t for t in said)
    assert any("Dr. A. Rossi" in t and "reviewed your case" in t and "ibuprofen" in t for t in said)
    say(client, "Can I take another ibuprofen tonight? The pain is back.")
    assert "asked you to avoid ibuprofen" in replies(client)[-1]  # the confirmed rule, next episode
    memory = {(m["layer"], m["partition"]): m for m in s["memory"]}
    marco = memory[(3, MARCO)]
    assert marco["chain"]["ok"] and marco["chain"]["length"] > 3
    assert [k["status"] for k in marco["knowledge"]] == ["confirmed"]
    assert [k["status"] for k in memory[(2, "hospital")]["knowledge"]] == ["confirmed"]


def test_golden_path_for_sofia(client: TestClient) -> None:
    for s in STEPS:
        step(client, s["n"], SOFIA)
    assert not items(client, "escalation", SOFIA)  # her script has no red flag
    pack = open_pack(client, SOFIA)
    assert {"radiologist_review", "nephrologist_review", "genetic_disclosure"} <= set(pack["reviews"])
    assert any(r["code"] == "vhl_followup" for r in pack["data"]["R"])
    r = client.get("/api/evidence/" + quote(pack["data"]["images"]["key_image"], safe=""))
    assert r.status_code == 200 and r.content[:4] == b"\x89PNG"
    sign(client, SOFIA)
    assert "reviewed your case" in replies(client, SOFIA)[-1]
    assert not replies(client, MARCO)  # each patient's phone stays their own


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
    pack = open_pack(client)
    r = act(client, pack["id"], "sign", "dr_rossi", items=pack["data"]["draft"])
    assert r.status_code == 409 and "review first: radiologist_review" in r.json()["error"]
    r = act(client, pack["id"], "review", "dr_rossi", gate="radiologist_review")
    assert r.status_code == 409 and "clinician:radiology" in r.json()["error"]
    assert act(client, pack["id"], "review", "dr_conti", gate="radiologist_review").status_code == 200
    assert act(client, pack["id"], "sign", "dr_rossi", items=pack["data"]["draft"]).status_code == 200


def test_edited_wording_is_exactly_what_the_patient_reads(client: TestClient) -> None:
    step(client, 1)
    pack = open_pack(client)
    first = pack["data"]["draft"][0]
    sign(client, items=[{"code": first["code"], "patient_text": "come at 8, Building C"}], note="Bring your results.")
    last = replies(client)[-1]
    assert "come at 8, Building C" in last and "Bring your results." in last
    assert first["patient_text"] not in last  # the draft wording was replaced, not added


def test_rejected_rule_is_not_used(client: TestClient) -> None:
    step(client, 1)
    step(client, 2)
    sign(client)
    rule = items(client, "rule", status="open")[0]
    assert act(client, rule["id"], "reject", "dr_rossi").status_code == 409  # a reason is required
    for rule in items(client, "rule", status="open"):
        assert act(client, rule["id"], "reject", "dr_rossi", reason="not for this patient").status_code == 200
    say(client, "Can I take another ibuprofen tonight? The pain is back.")
    assert "avoid ibuprofen" not in replies(client)[-1]


def test_escalation_moves_up_the_ladder_when_nobody_answers(client: TestClient) -> None:
    step(client, 1)
    session = client.app.state.session  # type: ignore[attr-defined]
    item = items(client, "escalation")[0]
    assert item["data"]["paged"] == ["Dr. A. Rossi"]

    async def overdue() -> None:
        from datetime import timedelta
        session.clock.start += timedelta(minutes=6)
        await session.desk.escalations.tick()

    client.portal.call(overdue)  # type: ignore[union-attr]
    moved = items(client, "escalation")[0]
    assert moved["id"] == item["id"] and moved["data"]["paged"] == ["Dr. A. Rossi", "Dr. L. Bianchi"]
    assert moved["data"]["level"] == 1


def test_sofia_failed_ct_cases_reach_the_board(client: TestClient) -> None:
    assert client.post("/api/patient/select", json={"patient": SOFIA}).status_code == 200
    s = state(client)
    assert s["patient"] == SOFIA and len(s["studies"]) == 11
    by_kind = {st["study_uid"].rsplit(".", 1)[1]: st["study_uid"] for st in s["studies"]}

    assert read_ct(client, by_kind["01"]).json()["status"] == "ok"
    assert signals(client, SOFIA)[0]["label"] == "normal"
    assert read_ct(client, by_kind["01"]).json()["duplicate"] is True
    assert len(signals(client, SOFIA)) == 1

    assert read_ct(client, by_kind["05"]).json()["status"] == "out_of_distribution"
    assert read_ct(client, by_kind["08"]).json() == {"status": "unreadable", "sent": False, "duplicate": False}
    assert read_ct(client, by_kind["09"]).json()["status"] == "model_error"
    titles = [i["title"] for i in items(client, "verification_failed", SOFIA)]
    assert any("Safety Verifier" in t for t in titles)
    assert "CT not read: unreadable" in titles and "CT not read: model_error" in titles

    r = read_ct(client, by_kind["11"])
    assert r.status_code == 422 and "CT only" in r.json()["error"]


def test_ocr_error_is_stopped_and_good_photo_reaches_the_pack(client: TestClient) -> None:
    say(client, "Prendo Brufen 600 tre volte al giorno", SOFIA)
    assert "referto" in replies(client, SOFIA)[-1]
    r = client.post("/api/patient/attach-lab", json={"photo": "ocr-error", "patient": SOFIA})
    assert r.status_code == 200
    failed = items(client, "verification_failed", SOFIA)
    assert failed and "eGFR plausible" in " ".join(failed[0]["data"]["failed"])
    r = client.post("/api/scenario/step", json={"n": 5, "patient": SOFIA, "wait": True})
    assert r.status_code == 409  # nothing verified reached the Case Briefer
    say(client, "Prendo Brufen 600 tre volte al giorno", SOFIA)
    client.post("/api/patient/attach-lab", json={"photo": "ok", "patient": SOFIA})
    assert "nephrologist_review" in open_pack(client, SOFIA)["reviews"]


def test_images_come_from_cortex(client: TestClient) -> None:
    read_ct(client)
    images = open_pack(client)["data"]["images"]
    for ref in (images["key_image"], images["heatmap_ref"]):
        r = client.get("/api/evidence/" + quote(ref, safe=""))  # as Chrislin's frontend asks for it
        assert r.status_code == 200 and r.headers["content-type"] == "image/png" and r.content[:4] == b"\x89PNG"
        by_patient = client.get(f"/api/evidence/{MARCO}/{ref.removeprefix('cortex:evidence/')}")
        assert by_patient.content == r.content
    assert client.get("/api/evidence/marco-r/not-an-id").status_code == 404
    assert client.get("/api/evidence/" + quote("cortex:evidence/nope", safe="")).status_code == 404
    assert client.get("/api/evidence/../secret/x").status_code in (404, 405)


def test_upload_with_the_stub_is_not_given_a_made_up_answer(client: TestClient) -> None:
    png = (client.app.state.session.root / "marco-r" / "ct-2026-10-11" / "key.png").read_bytes()  # type: ignore[attr-defined]
    other = png + b"\0"  # still a valid PNG, but not the study the stub knows
    r = client.post("/api/pacs/upload", json={"name": "../../evil.png", "wait": True,
                                              "data_base64": base64.b64encode(other).decode()})
    assert r.status_code == 200 and r.json()["status"] == "model_error"
    assert not signals(client)
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
    monkeypatch.setattr(web_session.Session, "_load_real_model", lambda self: _nothing())
    desk = client.app.state.session  # type: ignore[attr-defined]
    desk._real = CallableCtModel(fake)
    assert client.post("/api/pacs/model", json={"mode": "real"}).status_code == 200
    assert state(client)["ct_model"]["mode"] == "real"
    assert read_ct(client).json()["status"] == "ok"
    data = signals(client)[0]
    assert data["label"] == "tumor" and data["provenance"]["live_model"] is True and "T=0.798" in data["calibration"]


async def _nothing() -> None:
    return None


def test_choosing_chrislins_model_loads_it_in_the_background(client: TestClient,
                                                            monkeypatch: pytest.MonkeyPatch) -> None:
    import sys
    import types

    from kcd.web import session as web_session

    # A stand-in for kcd.imaging.chrislin_model: the real module needs scipy, which CI does not install.
    loaded = []
    fake = types.ModuleType("kcd.imaging.chrislin_model")
    fake.load = lambda: loaded.append("load")
    fake.engine = lambda: "openvino" if loaded else None
    monkeypatch.setitem(sys.modules, "kcd.imaging.chrislin_model", fake)
    monkeypatch.setattr(sys.modules["kcd.imaging"], "chrislin_model", fake, raising=False)
    monkeypatch.setattr(web_session, "real_model_available", lambda: True)
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
    for s in STEPS:
        step(client, s["n"])
    say(client, "Hello", SOFIA)
    r = client.post("/api/patient/erase", json={"patient": MARCO})
    assert r.status_code == 200 and r.json()["complete"] is True and r.json()["evidence_rows"] > 0
    s = state(client)
    assert not [i for i in s["board"] if i["patient_id"] == MARCO]
    assert not [m for m in s["memory"] if m["layer"] == 3 and m["partition"] == MARCO]
    assert not replies(client, MARCO) and replies(client, SOFIA)  # only Marco is erased
    assert not [e for e in events(client, "a2a") if e["patient"] == MARCO]


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
    assert "Case Briefer is now v2" in ask("Case Briefer: never diagnose a patient.")["text"]
    created = ask("Create an agent for nephrology phone follow-up: it calls CKD patients on WhatsApp. It must never "
                  "change a therapy; every piece of advice must be approved by the nephrologist.")
    assert "shadow" in created["text"]
    seat = next(s for s in state(client)["seats"] if s["id"] == created["seat"])
    assert seat["shadow"] is True
    assert "did not understand" in ask("Good morning everybody, lovely weather")["text"]


def test_bad_requests_get_a_readable_error(client: TestClient) -> None:
    assert client.post("/api/patient/message", json={"text": " "}).status_code == 422
    assert client.post("/api/patient/message", content="nope").status_code == 400
    assert client.post("/api/patient/message", json={"text": "hi", "patient": "nobody"}).status_code == 404
    assert act(client, "x", "sign", "dr_rossi").status_code == 404
    assert act(client, "x", "sign", "dr_nobody").status_code == 422
    assert client.post("/api/scenario/step", json={"n": 99}).status_code == 422
    r = client.post("/api/scenario/step", json={"n": 5, "patient": SOFIA, "wait": True})
    assert r.status_code == 409 and "nothing to put in a case pack" in r.json()["error"]
