import base64
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import numpy as np
from kcd_core.contracts import MessageType
from kcd_core.contracts.messages import ImagingSignal
from kcd_core.pep import PolicyViolation

from kcd.cortex import Cortex
from kcd.desk import Desk
from kcd.imaging import heatmap as hm
from kcd.imaging.calibration import Calibration, ece, fit_temperature, softmax
from kcd.imaging.image_io import ImageDecodeError, load_image
from kcd.imaging.model import (
    CT_LABELS,
    CallableCtModel,
    CtModelError,
    FixedCtModel,
    HttpCtModel,
    prediction_from_dict,
)
from kcd.imaging.pacs import DicomWebPacs, FolderPacs, PacsError
from kcd.imaging.quality import check_image, check_prediction
from kcd.seats.imaging_reader import ImagingReader, NoStudy
from kcd.tools import imaging_tools
from tests.helpers import DEMO_CT, png_bytes, synthetic_ct, write_study

TUMOR = {"Normal": 0.02, "Cyst": 0.03, "Stone": 0.05, "Tumor": 0.90}


class CalibrationTest(unittest.TestCase):
    def setUp(self):
        rng = np.random.default_rng(7)
        self.true_logits = rng.normal(0, 1.5, size=(4000, 4))
        p = softmax(self.true_logits)
        self.labels = np.array([rng.choice(4, p=row) for row in p])

    def test_recovers_overconfidence_temperature(self):
        t = fit_temperature(self.true_logits * 3.0, self.labels)
        self.assertAlmostEqual(t, 3.0, delta=0.25)

    def test_fit_reduces_ece_and_round_trips(self):
        cal = Calibration.fit(self.true_logits * 3.0, self.labels, "m1")
        self.assertLess(cal.ece_after, cal.ece_before)
        with tempfile.TemporaryDirectory() as d:
            cal.save(Path(d) / "cal.json")
            self.assertEqual(Calibration.load(Path(d) / "cal.json"), cal)

    def test_temperature_keeps_argmax(self):
        z = np.array([[0.1, 2.0, -1.0, 0.5]])
        self.assertEqual(int(softmax(z, 0.3).argmax()), int(softmax(z, 5.0).argmax()))

    def test_validation(self):
        with self.assertRaises(ValueError):
            fit_temperature(np.zeros((5, 4)), np.zeros(5, dtype=int))           # too few samples
        with self.assertRaises(ValueError):
            fit_temperature(np.zeros((30, 4)), np.full(30, 4))                  # label out of range
        with self.assertRaises(ValueError):
            fit_temperature(np.full((30, 4), np.nan), np.zeros(30, dtype=int))  # non-finite
        with self.assertRaises(ValueError):
            Calibration(temperature=0, model_version="m")
        with self.assertRaises(ValueError):
            Calibration(temperature=1.0, model_version="")

    def test_ece_perfect_and_bad(self):
        self.assertAlmostEqual(ece(np.array([[1.0, 0.0], [0.0, 1.0]]), np.array([0, 1])), 0.0)
        self.assertAlmostEqual(ece(np.array([[1.0, 0.0], [1.0, 0.0]]), np.array([1, 1])), 1.0)


class ImageAndQualityTest(unittest.TestCase):
    def test_synthetic_ct_passes(self):
        q = check_image(load_image(synthetic_ct()))
        self.assertTrue(q["ok"], q)

    def test_demo_ct_passes(self):
        self.assertTrue(check_image(load_image(DEMO_CT.read_bytes()))["ok"])

    def test_failures(self):
        tiny = check_image(load_image(png_bytes(np.full((64, 64), 120, np.uint8))))
        failed = {c["name"] for c in tiny["checks"] if not c["ok"]}
        self.assertEqual(failed, {"min_side_px", "contrast_std"})
        colour = np.zeros((256, 256, 3), np.uint8)
        colour[..., 0] = 255
        colour[:128, :, 1] = 200
        failed = {c["name"] for c in check_image(load_image(png_bytes(colour)))["checks"] if not c["ok"]}
        self.assertIn("grayscale", failed)
        wide = check_image(load_image(png_bytes(np.tile(np.arange(256, dtype=np.uint8), (128, 3)))))
        self.assertIn("aspect_ratio", {c["name"] for c in wide["checks"] if not c["ok"]})

    def test_decode_errors(self):
        with self.assertRaises(ImageDecodeError):
            load_image(b"not an image")
        with self.assertRaises(ImageDecodeError):
            load_image(b"")
        fake_dicom = b"\x00" * 128 + b"DICM" + b"\x00" * 64
        with self.assertRaises(ImageDecodeError):
            load_image(fake_dicom)  # without pydicom, or with garbage data: a clear decode error

    def test_16bit_png(self):
        arr = (np.arange(256 * 256, dtype=np.uint16).reshape(256, 256))
        img = load_image(png_bytes(arr))
        self.assertAlmostEqual(float(img.gray.min()), 0.0)
        self.assertAlmostEqual(float(img.gray.max()), 1.0)

    def test_prediction_checks(self):
        ok = {c["name"]: c["ok"] for c in check_prediction(np.array([0.9, 0.05, 0.03, 0.02]))}
        self.assertEqual(ok, {"top_probability": True, "normalized_entropy": True})
        flat = {c["name"]: c["ok"] for c in check_prediction(np.full(4, 0.25))}
        self.assertEqual(flat, {"top_probability": False, "normalized_entropy": False})


class HeatmapTest(unittest.TestCase):
    def test_metrics_for_left_central_blob(self):
        h = np.zeros((100, 100), np.float32)
        h[40:60, 30:45] = 1.0
        m = hm.cam_metrics(h)
        self.assertAlmostEqual(m["coverage"], 0.03)
        self.assertAlmostEqual(m["center_ratio"], 1.0)
        self.assertAlmostEqual(m["lr_asymmetry"], 1.0)
        self.assertAlmostEqual(m["tb_asymmetry"], 0.0)
        self.assertEqual(m["peak"], [0.305, 0.405])

    def test_empty_negative_and_bad_maps(self):
        self.assertTrue(hm.cam_metrics(hm.normalize(np.full((8, 8), -1.0)))["empty"])
        with self.assertRaises(ValueError):
            hm.normalize(np.zeros(5))
        with self.assertRaises(ValueError):
            hm.normalize(np.array([[np.inf]]))

    def test_resize_and_overlay(self):
        heat = hm.resize_to(hm.normalize(np.eye(16)), 64, 48)
        self.assertEqual(heat.shape, (48, 64))
        self.assertLessEqual(float(heat.max()), 1.0)
        png = hm.overlay_png(np.zeros((48, 64), np.float32), heat)
        self.assertEqual(load_image(png).width, 64)
        with self.assertRaises(ValueError):
            hm.overlay_png(np.zeros((10, 10)), heat)


class ModelAdapterTest(unittest.TestCase):
    def test_labels_any_order_and_probs_to_logits(self):
        p = prediction_from_dict({"model_version": "m1", "labels": ["Tumor", "Stone", "Normal", "Cyst"],
                                  "probs": [0.7, 0.1, 0.15, 0.05]})
        probs = softmax(p.logits)
        self.assertEqual(dict(zip(CT_LABELS, np.round(probs, 4), strict=True)),
                         {"normal": 0.15, "cyst": 0.05, "stone": 0.1, "tumor": 0.7})
        self.assertEqual(p.output_kind, "probs")

    def test_rejects_bad_outputs(self):
        base = {"model_version": "m", "labels": ["normal", "cyst", "stone", "tumor"]}
        bad = [
            {**base, "labels": ["normal", "cyst", "stone"], "logits": [1, 2, 3]},
            {**base, "labels": ["normal", "normal", "stone", "tumor"], "logits": [1, 2, 3, 4]},
            {**base, "labels": ["normal", "cyst", "stone", "mass"], "logits": [1, 2, 3, 4]},
            {**base, "logits": [1, 2, 3, 4], "probs": [0.25] * 4},
            {**base},
            {**base, "logits": [1, 2, float("nan"), 4]},
            {**base, "logits": [1, 2, 3]},
            {**base, "logits": [1, 2, True, 4]},
            {**base, "probs": [0.5, 0.5, 0.5, 0.5]},
            {**base, "model_version": " ", "logits": [1, 2, 3, 4]},
            {**base, "logits": [1, 2, 3, 4], "heatmap": [1, 2, 3]},
            {**base, "logits": [1, 2, 3, 4], "heatmap_png_b64": "!!!"},
            "not a dict",
        ]
        for d in bad:
            with self.assertRaises(CtModelError, msg=str(d)[:80]):
                prediction_from_dict(d)

    def test_heatmap_png_b64(self):
        b64 = base64.b64encode(png_bytes(np.full((20, 30), 128, np.uint8))).decode()
        p = prediction_from_dict({"model_version": "m", "labels": list(CT_LABELS), "logits": [0, 0, 0, 1],
                                  "heatmap_png_b64": b64})
        self.assertEqual(p.heatmap.shape, (20, 30))

    def test_callable_model_gets_a_copy_and_errors_are_wrapped(self):
        img = load_image(synthetic_ct())
        before = img.gray.copy()

        def model(gray):
            gray[:] = 0  # a misbehaving model must not change our pixels
            return {"model_version": "m", "labels": list(CT_LABELS), "logits": [0, 0, 0, 1]}

        CallableCtModel(model).predict(img, b"", "image/png")
        self.assertTrue(np.array_equal(img.gray, before))

        def broken(gray):
            raise MemoryError("GPU out of memory")

        with self.assertRaises(CtModelError):
            CallableCtModel(broken).predict(img, b"", "image/png")
        with self.assertRaises(ValueError):
            CallableCtModel("no_colon_here")
        self.assertIsNotNone(CallableCtModel("tests.helpers:synthetic_ct").fn)

    def test_fixed_model_is_always_labelled_stub(self):
        self.assertIn("stub", FixedCtModel(TUMOR, model_version="v1").model_version)


class _Handler(BaseHTTPRequestHandler):
    routes: dict = {}

    def log_message(self, *args):
        pass

    def _reply(self, status, body, ctype):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        key = (u.path, tuple(sorted((k, v[0]) for k, v in parse_qs(u.query).items())))
        status, body, ctype = self.routes.get(key, self.routes.get(u.path, (404, b"", "text/plain")))
        self._reply(status, body, ctype)

    def do_POST(self):
        length = int(self.headers["Content-Length"])
        req = json.loads(self.rfile.read(length))
        type(self).last_post = req
        status, body, ctype = self.routes.get(("POST", self.path), (404, b"", "text/plain"))
        self._reply(status, body, ctype)


class LocalServer:
    def __init__(self, routes):
        handler = type("H", (_Handler,), {"routes": routes})
        self.handler = handler
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def dj(**tags):
    return {tag: {"vr": "XX", "Value": [value]} for tag, value in tags.items()}


class HttpAdaptersTest(unittest.TestCase):
    def test_http_ct_model(self):
        body = json.dumps({"model_version": "kidney-ct-cls 1.0", "labels": list(CT_LABELS),
                           "logits": [0.1, 0.2, 0.3, 3.0]}).encode()
        srv = LocalServer({("POST", "/classify"): (200, body, "application/json"),
                           ("POST", "/broken"): (500, b"boom", "text/plain")})
        try:
            img_bytes = synthetic_ct()
            p = HttpCtModel(srv.url + "/classify").predict(load_image(img_bytes), img_bytes, "image/png")
            self.assertEqual(p.model_version, "kidney-ct-cls 1.0")
            self.assertEqual(base64.b64decode(srv.handler.last_post["image_b64"]), img_bytes)
            with self.assertRaises(CtModelError):
                HttpCtModel(srv.url + "/broken").predict(load_image(img_bytes), img_bytes, "image/png")
        finally:
            srv.close()
        with self.assertRaises(ValueError):
            HttpCtModel("ftp://x")

    def test_dicomweb_pacs(self):
        studies = [
            dj(**{"0020000D": "1.2.old", "00100020": "marco-r", "00080020": "20250101", "00080061": "CT"}),
            dj(**{"0020000D": "1.2.new", "00100020": "marco-r", "00080020": "20261011", "00080030": "182000.5",
                  "00081030": "CT abdomen", "00080061": "CT"}),
            dj(**{"0020000D": "1.2.mr", "00100020": "marco-r", "00080020": "20261012", "00080061": "MR"}),
            dj(**{"0020000D": "1.2.other", "00100020": "marco-rx", "00080020": "20261013", "00080061": "CT"}),
        ]
        instances = [dj(**{"0020000E": "s-big", "00080018": f"i{n}", "00200013": str(n)}) for n in (3, 1, 2)]
        instances.append(dj(**{"0020000E": "s-small", "00080018": "x1", "00200013": "1"}))
        png = synthetic_ct()
        srv = LocalServer({
            ("/studies", (("PatientID", "marco-r"),)): (200, json.dumps(studies).encode(), "application/dicom+json"),
            ("/studies", (("PatientID", "nobody"),)): (204, b"", "application/dicom+json"),
            "/studies/1.2.new/instances": (200, json.dumps(instances).encode(), "application/dicom+json"),
            "/studies/1.2.new/series/s-big/instances/i2/rendered": (200, png, "image/png"),
            "/studies/1.2.empty/instances": (200, b"[]", "application/dicom+json"),
        })
        try:
            pacs = DicomWebPacs(srv.url)
            found = pacs.list_studies("marco-r")
            # CT only, exact patient match, newest first
            self.assertEqual([s.study_uid for s in found], ["1.2.new", "1.2.old"])
            self.assertEqual(found[0].acquired, "2026-10-11T18:20:00")
            data, media = pacs.fetch_key_image(found[0])  # middle instance of the largest series
            self.assertEqual((data, media), (png, "image/png"))
            self.assertEqual(pacs.list_studies("nobody"), [])
            with self.assertRaises(PacsError):
                pacs.fetch_key_image(found[1])  # 404
            empty = type(found[0])("1.2.empty", "marco-r", "CT", "")
            with self.assertRaises(PacsError):
                pacs.fetch_key_image(empty)
        finally:
            srv.close()


class Down:
    """A CT model service that is unavailable."""

    model_version = "m"

    def predict(self, *a):
        raise CtModelError("service unavailable")


class ReaderTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.image = synthetic_ct()
        write_study(self.root, "pt_marco", "1.2.3", self.image)
        self.desk = await Desk.create()
        self.cortex = Cortex()

    async def asyncTearDown(self):
        await self.desk.close()
        self.tmp.cleanup()

    async def reader(self, model=None, calibration="default"):
        model = model or FixedCtModel(TUMOR)
        if calibration == "default":
            calibration = Calibration(2.0, getattr(model, "model_version", "m"))
        seat = ImagingReader(self.cortex, calibration, audit=self.desk.audit, clock=self.desk.clock)
        rt = await self.desk.add_seat(seat, imaging_tools(FolderPacs(self.root), model))
        return seat, rt.context()

    async def process(self, seat, ctx, patient="pt_marco", **kw):
        result = await seat.process_study(ctx, patient_id=patient, episode_id="ep_1", **kw)
        await self.desk.bus.drain()
        return result

    def signals(self):
        return self.desk.delivered_to("safety_verifier")

    async def test_happy_path(self):
        seat, ctx = await self.reader()
        r = await self.process(seat, ctx)
        self.assertEqual((r.status, r.sent), ("ok", True))
        env = self.signals()[0]
        self.assertEqual((env.type, env.from_, env.to),
                         (MessageType.IMAGING_SIGNAL, "imaging_reader", "safety_verifier"))
        sig = ImagingSignal.model_validate(env.payload)  # Core's frozen contract
        self.assertEqual((sig.label, sig.quality, sig.study_uid), ("tumor", "ok", "1.2.3"))
        self.assertLess(sig.p_calibrated, sig.p_raw)  # T = 2 softens 0.90
        self.assertTrue(sig.heatmap_ref.startswith("cortex:evidence/"))
        self.assertIn(r.details_evidence_id, sig.technical_note)
        self.assertAlmostEqual(sum(r.details["probabilities"].values()), 1.0, places=3)
        self.assertEqual(self.desk.bus.dead_letters, [])
        kinds = [e["kind"] for e in self.cortex.list_evidence(3, "pt_marco")]
        self.assertEqual(kinds, ["ct_key_image", "ct_heatmap_overlay", "imaging_signal_details"])
        self.assertTrue(self.cortex.verify_chain(3, "pt_marco")["ok"])
        self.assertTrue(self.desk.audit.verify_chain())

    async def test_original_image_is_stored_unaltered(self):
        seat, ctx = await self.reader()
        r = await self.process(seat, ctx)
        key_ev = self.cortex.get_evidence(3, "pt_marco", r.details["provenance"]["key_image_evidence_id"])
        self.assertEqual(self.cortex.read_blob(3, "pt_marco", key_ev["blob_sha256"]), self.image)
        self.assertEqual((self.root / "pt_marco" / "1_2_3" / "key.png").read_bytes(), self.image)

    async def test_uncertain_prediction_is_out_of_distribution(self):
        seat, ctx = await self.reader(FixedCtModel({"normal": 0.3, "cyst": 0.25, "stone": 0.25, "tumor": 0.2}))
        r = await self.process(seat, ctx)
        self.assertEqual(r.status, "out_of_distribution")
        self.assertEqual(ImagingSignal.model_validate(self.signals()[0].payload).quality, "out_of_distribution")

    async def test_poor_image_gives_low_quality_signal(self):
        write_study(self.root, "pt_anna", "7.7", png_bytes(np.full((64, 64), 120, np.uint8)))
        seat, ctx = await self.reader()
        r = await self.process(seat, ctx, patient="pt_anna")
        self.assertEqual((r.status, r.sent), ("low", True))
        failed = {c["name"] for c in r.details["quality_checks"] if not c["ok"]}
        self.assertEqual(failed, {"min_side_px", "contrast_std"})

    async def test_unreadable_image_is_loud_not_silent(self):
        write_study(self.root, "pt_anna", "9.9", b"garbage")
        seat, ctx = await self.reader()
        r = await self.process(seat, ctx, patient="pt_anna")
        self.assertEqual((r.status, r.sent, r.message_id), ("unreadable", False, None))
        self.assertEqual(self.signals(), [])
        self.assertEqual(self.desk.audit.find("imaging.no_signal", "error")[0].detail["status"], "unreadable")
        details = self.cortex.get_evidence(3, "pt_anna", r.details_evidence_id)
        self.assertIn("cannot decode", details["content"]["error"])

    async def test_model_failure_is_loud_not_silent(self):
        seat, ctx = await self.reader(Down())
        r = await self.process(seat, ctx)
        self.assertEqual((r.status, r.sent), ("model_error", False))
        self.assertIn("service unavailable", r.details["error"])
        self.assertEqual(self.desk.audit.find("imaging.no_signal", "error")[0].detail["status"], "model_error")
        self.assertEqual(self.signals(), [])

    async def test_calibration_states(self):
        seat, ctx = await self.reader(calibration=None)
        r = await self.process(seat, ctx)
        self.assertEqual(r.details["calibration"]["status"], "uncalibrated")
        self.assertEqual(r.details["p_calibrated"], r.details["p_raw"])
        seat.calibration = Calibration(2.0, "another-model")
        r = await self.process(seat, ctx, force=True)
        self.assertEqual(r.details["calibration"]["status"], "stale")
        self.assertEqual(r.details["p_calibrated"], r.details["p_raw"])

    async def test_study_selection(self):
        write_study(self.root, "pt_marco", "1.2.4", self.image, acquired="2026-10-20T08:00:00Z")
        write_study(self.root, "pt_marco", "1.2.5", self.image, acquired="2026-10-21T08:00:00Z", modality="MR")
        seat, ctx = await self.reader()
        self.assertEqual((await self.process(seat, ctx)).details["provenance"]["study_uid"], "1.2.4")
        picked = await self.process(seat, ctx, study_uid="1.2.3")
        self.assertEqual(picked.details["provenance"]["study_uid"], "1.2.3")
        with self.assertRaises(NoStudy):
            await self.process(seat, ctx, study_uid="1.2.5")  # MR is not a CT
        with self.assertRaises(NoStudy):
            await self.process(seat, ctx, patient="nobody")
        with self.assertRaises(PacsError):
            await self.process(seat, ctx, patient="../etc")

    async def test_idempotent_unless_forced(self):
        seat, ctx = await self.reader()
        first = await self.process(seat, ctx)
        again = await self.process(seat, ctx)
        self.assertTrue(again.duplicate)
        self.assertEqual(again.message_id, first.message_id)
        self.assertEqual(len(self.signals()), 1)
        await self.process(seat, ctx, force=True)
        self.assertEqual(len(self.signals()), 2)

    async def test_policy(self):
        seat, ctx = await self.reader()
        for tool, refusal in (("pacs.write_study", "no_image_alteration"),
                              ("channel.send_message", "no_patient_contact")):
            with self.assertRaises(PolicyViolation) as cm:
                ctx.pep.check_tool(tool)
            self.assertEqual(cm.exception.refusal, refusal)
        with self.assertRaises(PolicyViolation) as cm:
            ctx.guard_content("diagnosis")  # a diagnosis needs a clinician release
        self.assertEqual(cm.exception.refusal, "no_diagnosis")
        with self.assertRaises(PolicyViolation):
            ctx.pep.check_emit(MessageType.IMAGING_SIGNAL, "case_briefer")  # only via the Safety Verifier
        with self.assertRaises(PolicyViolation):
            ctx.check_memory("L2", "read")
        self.assertFalse(any(hasattr(FolderPacs, m) for m in ("write", "store", "put", "delete")))

    async def test_policy_violation_from_a_tool_is_not_swallowed_as_model_error(self):
        seat, ctx = await self.reader()
        registry = self.desk.registry
        without_model = registry.get("imaging_reader").model_copy(
            update={"tools": ["pacs.list_studies", "pacs.fetch_study"]})
        registry.activate(without_model)  # hot reload: ct.classify is no longer allowed
        with self.assertRaises(PolicyViolation):
            await self.process(seat, ctx)
        self.assertEqual(self.signals(), [])


if __name__ == "__main__":
    unittest.main()
