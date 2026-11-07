"""Web layer (Person 5): the desk behind a browser. Light mode, so no Cortex / sqlalchemy is needed; the full
desk is exercised by `python -m kcd.web` and kcd.demo."""

import asyncio
import json
import unittest

from kcd.web.app import ActionError, DeskApp
from kcd.web.server import serve


async def http(port: int, method: str, path: str, body: dict | None = None) -> tuple[int, bytes]:
    r, w = await asyncio.open_connection("127.0.0.1", port)
    raw = json.dumps(body).encode() if body is not None else b""
    w.write(f"{method} {path} HTTP/1.1\r\nHost: x\r\nContent-Length: {len(raw)}\r\n\r\n".encode() + raw)
    await w.drain()
    data = await r.read()
    w.close()
    head, _, payload = data.partition(b"\r\n\r\n")
    return int(head.split()[1]), payload


class WebAppTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.app = await DeskApp.create(light=True)

    async def asyncTearDown(self):
        await self.app.close()

    def items(self, kind):
        return [i for i in self.app.board.items if i["kind"] == kind]

    async def test_red_flag_pages_and_opens_an_escalation_to_acknowledge(self):
        await self.app.run_step(1)
        esc = self.items("escalation")
        self.assertEqual(len(esc), 1)
        self.assertEqual(esc[0]["status"], "open")
        self.assertIn("Dr. A. Rossi", esc[0]["data"]["paged"])
        await self.app.board_act(esc[0]["id"], "acknowledge", "dr_rossi")
        self.assertEqual(self.app.board.get(esc[0]["id"])["status"], "acknowledged")
        kinds = {e["kind"] for e in self.app.hub.history}
        self.assertTrue({"patient_in", "patient_out", "a2a", "page", "gate", "humanized"} <= kinds)

    async def test_full_path_to_a_signed_decision_the_patient_hears(self):
        await self.app.run_step(1)
        await self.app.run_step(2)
        await self.app.run_step(5)
        pack = next(i for i in self.items("case_pack") if i["status"] == "open")
        self.assertIn("nephrologist_review", pack["required_gates"])
        with self.assertRaises(ActionError):  # review first
            await self.app.board_act(pack["id"], "sign", "dr_rossi")
        with self.assertRaises(ActionError):  # wrong role
            await self.app.board_act(pack["id"], "review", "dr_neri", gate="nephrologist_review")
        await self.app.board_act(pack["id"], "review", "dr_rossi", gate="nephrologist_review")
        sel = [{"code": d["code"], "selected": d["code"] == "stop_nsaid"} for d in pack["data"]["draft"]]
        sel[[s["code"] for s in sel].index("stop_nsaid")]["patient_text"] = "Please stop ibuprofen today."
        await self.app.board_act(pack["id"], "sign", "dr_rossi", items=sel, note="")
        self.assertEqual(self.app.board.get(pack["id"])["status"], "signed")
        self.assertEqual(self.app.companion.told_patient[0]["text"], "Please stop ibuprofen today.")
        last = [e for e in self.app.hub.history if e["kind"] == "patient_out"][-1]
        self.assertIn("Please stop ibuprofen today.", last["data"]["text"])
        self.assertTrue(self.app.state()["integrity"]["audit_chain_ok"])
        self.assertEqual(self.app.state()["integrity"]["dead_letters"], 0)

    async def test_two_patients_side_by_side(self):
        state = self.app.state()
        self.assertEqual([p["id"] for p in state["patients"]], ["marco-r", "sofia-b"])
        self.assertEqual([p["age"] for p in state["patients"]], [48, 57])
        await self.app.run_step(1, "marco-r")
        await self.app.run_step(1, "sofia-b")
        # Marco's red flag pages the on-call doctor; Sofia's quieter story does not
        esc = self.items("escalation")
        self.assertEqual([e["patient_id"] for e in esc], ["marco-r"])
        for pid in ("marco-r", "sofia-b"):
            await self.app.run_step(2, pid)
            await self.app.run_step(5, pid)
        packs = {p["patient_id"]: p for p in self.items("case_pack")}
        self.assertEqual(set(packs), {"marco-r", "sofia-b"})
        self.assertNotEqual(packs["marco-r"]["case_id"], packs["sofia-b"]["case_id"])
        self.assertIn("Sofia", " ".join(packs["sofia-b"]["sbar"]["B"]))
        self.assertIn("eGFR 49", " ".join(packs["sofia-b"]["sbar"]["A"]))  # her own lab photo, not Marco's
        self.assertIn("eGFR 52", " ".join(packs["marco-r"]["sbar"]["A"]))
        sofia = packs["sofia-b"]
        for g in [g for g, r in sofia["reviews"].items() if r is None]:
            role = {"nephrologist_review": "dr_rossi", "radiologist_review": "dr_neri", "genetic_disclosure": "dr_gallo"}[g]
            await self.app.board_act(sofia["id"], "review", role, gate=g)
        await self.app.board_act(sofia["id"], "sign", "dr_rossi")
        self.assertEqual(self.app.board.get(sofia["id"])["status"], "signed")
        self.assertEqual(self.app.board.get(packs["marco-r"]["id"])["status"], "open")  # Marco's is untouched
        outs = [e["data"] for e in self.app.hub.history if e["kind"] == "patient_out"]
        self.assertTrue(all(o.get("patient") in ("marco-r", "sofia-b") for o in outs))
        with self.assertRaises(ActionError):
            await self.app.run_step(1, "nobody")

    async def test_companion_refuses_the_ct_result_without_a_release(self):
        await self.app.patient_say("I had a CT scan yesterday: what did it show?")
        out = [e["data"]["text"] for e in self.app.hub.history if e["kind"] == "patient_out"]
        self.assertTrue(out)
        self.assertNotIn("tumor", " ".join(out).lower())

    async def test_reset_clears_everything(self):
        await self.app.run_step(1)
        await self.app.reset()
        self.assertEqual(self.app.board.items, [])
        self.assertEqual([e["kind"] for e in self.app.hub.history], ["reset"])

    async def test_http_api_and_static(self):
        server = await serve(self.app, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        async with server:
            code, body = await http(port, "GET", "/api/state")
            state = json.loads(body)
            self.assertEqual((code, state["patient"], state["light"]), (200, "marco-r", True))
            self.assertEqual({s["id"] for s in state["seats"]} >= {"companion", "case_briefer", "dispatcher", "chief_of_staff"}, True)
            code, _ = await http(port, "POST", "/api/patient/message", {"text": "hello"})
            self.assertEqual(code, 200)
            code, body = await http(port, "POST", "/api/patient/message", {"text": " "})
            self.assertEqual((code, json.loads(body)["ok"]), (400, False))
            code, body = await http(port, "POST", "/api/board/act",
                                    {"item": "nope", "action": "sign", "clinician": "dr_rossi"})
            self.assertEqual(code, 400)
            code, _ = await http(port, "POST", "/api/nothing", {})
            self.assertEqual(code, 404)
            code, html = await http(port, "GET", "/")
            self.assertEqual(code, 200)
            self.assertIn(b"Kidney Care Desk", html)
            for path in ("/app.js", "/app.css", "/assets/loopnexus.png"):
                self.assertEqual((await http(port, "GET", path))[0], 200, path)
            self.assertEqual((await http(port, "GET", "/../../etc/passwd"))[0], 404)
            code, _ = await http(port, "POST", "/api/scenario/step", {"n": 1})
            self.assertEqual(code, 202)
            for _ in range(100):
                await asyncio.sleep(0.05)
                if self.app.running is None and self.items("escalation"):
                    break
            self.assertTrue(self.items("escalation"))


if __name__ == "__main__":
    unittest.main()
