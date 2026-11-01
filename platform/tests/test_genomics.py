import json
import tempfile
import unittest
from pathlib import Path

from kcd_core.contracts import MessageType
from kcd_core.contracts.messages import DataDelivered
from kcd_core.pep import PolicyViolation

from kcd.cortex import Cortex
from kcd.desk import Desk
from kcd.genomics.panel import PanelError, normalize_classification, parse_panel_json, parse_vcf, zygosity_from_gt
from kcd.genomics.rules import RulesError, VariantRules, interpret
from kcd.genomics.source import FolderPanelSource
from kcd.seats.genomics_interpreter import GenomicsInterpreter, history_from_summary
from kcd.tools import genomics_tools
from tests.helpers import DEMO_PANEL

RCC = {"history": {"rcc": True}}

VCF = """##fileformat=VCFv4.2
##fileDate=20240515
##panel_id=NGS-TEST-1
##patient_id=anna-b
#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tSAMPLE
chr3\t10183812\trs5030827\tC\tT\t.\tPASS\tGENEINFO=VHL:7428;CLNSIG=Likely_pathogenic;CLNDN=Von_Hippel-Lindau_syndrome\tGT\t0/1
chr22\t36265860\trs73885319\tA\tG\t.\tPASS\tGENEINFO=APOL1:8542;CLNSIG=risk_factor\tGT:DP\t1/1:40
chr2\t1\t.\tG\tA\t.\tPASS\tGENE=FH;CLNSIG=Pathogenic\tGT\t0/0
chr1\t2\t.\tG\tA\t.\tPASS\tGENE=XYZ1;CLNSIG=Pathogenic/Likely_pathogenic\tGT\t0|1
"""


def panel(variants, patient="p1"):
    return parse_panel_json({"patient_id": patient, "genomic_profile_id": "G1", "variants": variants})


def v(gene, sig, zyg="heterozygous", vid=""):
    return {"gene": gene, "clinical_significance": sig, "zygosity": zyg, "variant_id": vid}


class PanelParsingTest(unittest.TestCase):
    def test_demo_panel(self):
        p = parse_panel_json(json.loads(DEMO_PANEL.read_text()))
        self.assertEqual(p.patient_id, "marco-r")
        self.assertEqual([x.classification for x in p.variants], ["pathogenic", "risk_allele", "vus", "benign"])
        self.assertEqual(p.variants[2].classification_raw, "VUS")  # verbatim

    def test_normalisation_never_guesses(self):
        self.assertEqual(normalize_classification("Uncertain significance"), "vus")
        self.assertEqual(normalize_classification("Likely-Pathogenic"), "likely_pathogenic")
        self.assertEqual(normalize_classification("Pathogenic/Likely_pathogenic"), "unrecognized")
        self.assertEqual(normalize_classification("drug response"), "unrecognized")

    def test_genotypes(self):
        cases = {"0/1": "heterozygous", "1|0": "heterozygous", "1/1": "homozygous", "1/2": "compound_heterozygous",
                 "1": "hemizygous", "0/0": "absent", "./.": "unknown", "./1": "unknown"}
        for gt, expected in cases.items():
            self.assertEqual(zygosity_from_gt(gt), expected, gt)

    def test_vcf(self):
        p = parse_vcf(VCF, "anna-b")
        self.assertEqual((p.panel_id, p.date, p.source_format), ("NGS-TEST-1", "2024-05-15", "vcf"))
        self.assertEqual([x.gene for x in p.variants], ["VHL", "APOL1", "XYZ1"])  # FH 0/0 is not carried
        self.assertEqual(p.variants[0].condition, "Von Hippel-Lindau syndrome")
        self.assertEqual(p.variants[1].zygosity, "homozygous")
        self.assertEqual(p.variants[2].hgvs, "chr1:2G>A")
        with self.assertRaises(PanelError):
            parse_vcf(VCF, "marco-r")  # file says it belongs to anna-b

    def test_bad_inputs(self):
        for bad in ({}, {"patient_id": "p", "variants": [{"gene": "VHL"}]}, {"variants": []}):
            with self.assertRaises(PanelError):
                parse_panel_json(bad)
        for bad in ("chr1\t1\t.\tA\tG\t.\tPASS\tGENE=X;CLNSIG=Benign", "#CHROM\tPOS\n",
                    "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\nchr1\t1\t.\tA\tG\t.\tPASS\tCLNSIG=Benign",
                    "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\nchr1\t1", "##only meta"):
            with self.assertRaises(PanelError, msg=bad[:40]):
                parse_vcf(bad, "p")


class RulesTest(unittest.TestCase):
    def setUp(self):
        self.rules = VariantRules.load()

    def by_gene(self, result):
        return {f["gene"]: f for f in result["findings"]}

    def test_marco(self):
        r = interpret(parse_panel_json(json.loads(DEMO_PANEL.read_text())), self.rules, RCC)
        f = self.by_gene(r)
        self.assertEqual({g: x["relevance"] for g, x in f.items()}, {"VHL": "high", "APOL1": "low", "PBRM1": "none"})
        self.assertEqual([x["gene"] for x in r["findings"]], ["VHL", "APOL1", "PBRM1"])  # most relevant first
        self.assertIn("kidney-sparing", " ".join(r["case_implications"]))
        self.assertEqual(r["not_reported"][0]["gene"], "BAP1")
        self.assertEqual(r["hereditary_syndromes"], ["von Hippel-Lindau disease"])
        self.assertTrue(all(x["citations"] for x in r["findings"] if x["gene"] != "APOL1") and f["APOL1"]["citations"])

    def test_history_dependent_implication(self):
        r = interpret(panel([v("VHL", "pathogenic")]), self.rules, {"history": {}})
        self.assertNotIn("kidney-sparing", " ".join(r["case_implications"]))
        self.assertIn("Family counselling", " ".join(r["case_implications"]))

    def test_apol1_counts_alleles(self):
        one = self.by_gene(interpret(panel([v("APOL1", "risk_allele")]), self.rules))["APOL1"]
        hom = self.by_gene(interpret(panel([v("APOL1", "risk_allele", "homozygous")]), self.rules))["APOL1"]
        compound = interpret(panel([v("APOL1", "risk_allele", vid="G1"), v("APOL1", "risk_allele", vid="G2")]),
                             self.rules)
        self.assertEqual((one["relevance"], one["risk_alleles_in_gene"]), ("low", 1))
        self.assertEqual((hom["relevance"], hom["risk_alleles_in_gene"]), ("moderate", 2))
        self.assertEqual({f["relevance"] for f in compound["findings"]}, {"moderate"})

    def test_unknown_genes_and_classes_go_to_a_geneticist(self):
        r = interpret(panel([v("ABC9", "pathogenic"), v("VHL", "drug response"), v("FH", "likely pathogenic")]),
                      self.rules)
        f = self.by_gene(r)
        self.assertEqual(f["ABC9"]["relevance"], "review")
        self.assertEqual(f["VHL"]["relevance"], "unknown")
        self.assertEqual(f["FH"]["relevance"], "high")
        self.assertTrue(r["needs_geneticist_review"])

    def test_lab_class_is_never_changed(self):
        p = panel([v("VHL", "Likely_pathogenic"), v("PBRM1", "Uncertain significance"), v("X", "weird")])
        r = interpret(p, self.rules)
        self.assertEqual([f["lab_classification"] for f in r["findings"]],
                         ["Likely_pathogenic", "weird", "Uncertain significance"])

    def test_rule_file_validation(self):
        good = json.loads(Path(VariantRules.load.__globals__["RULES_PATH"]).read_text())
        broken = json.loads(json.dumps(good))
        broken["gene_rules"][0]["relevance"] = "urgent"
        with self.assertRaises(RulesError):
            VariantRules.from_dict(broken)
        broken = json.loads(json.dumps(good))
        broken["gene_rules"][0]["citations"] = ["NOPE"]
        with self.assertRaises(RulesError):
            VariantRules.from_dict(broken)
        broken = json.loads(json.dumps(good))
        del broken["class_defaults"]["vus"]
        with self.assertRaises(RulesError):
            VariantRules.from_dict(broken)


GENOMICS_REQUEST = {"items": [{"kind": "genomics", "code": "genomic_context"}], "reason": "case pack"}


class InterpreterSeatTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        panel = json.loads(DEMO_PANEL.read_text())
        panel["patient_id"] = "pt_marco"
        (self.root / "pt_marco").mkdir()
        (self.root / "pt_marco" / "panel.json").write_text(json.dumps(panel))
        self.desk = await Desk.create()
        self.cortex = Cortex()
        self.seat = GenomicsInterpreter(self.cortex)
        self.tools = genomics_tools(FolderPanelSource(self.root),
                                    patients={"pt_marco": {"conditions": ["Clear cell renal cell carcinoma"]}})
        self.rt = await self.desk.add_seat(self.seat, self.tools)

    async def asyncTearDown(self):
        await self.desk.close()
        self.tmp.cleanup()

    async def request(self, payload=None, patient="pt_marco"):
        env = self.desk.envelope(MessageType.DATA_REQUEST, "case_briefer", "genomics_interpreter",
                                 payload or GENOMICS_REQUEST, patient=patient)
        await self.desk.deliver(env)
        return env

    def delivered(self):
        return self.desk.delivered_to("safety_verifier")

    def blocked(self):
        return self.desk.audit.find("seat.policy_violation", "blocked")

    async def test_data_request_to_genomic_context(self):
        req = await self.request()
        self.assertEqual(self.desk.bus.dead_letters, [])
        env = self.delivered()[0]
        self.assertEqual((env.type, env.from_), (MessageType.DATA_DELIVERED, "genomics_interpreter"))
        dd = DataDelivered.model_validate(env.payload)  # Core's frozen contract
        self.assertEqual((dd.kind, dd.in_reply_to), ("genomic_context", req.id))
        summary, *findings = dd.items
        self.assertEqual(summary["type"], "panel_summary")
        self.assertEqual({f["gene"]: f["relevance"] for f in findings},
                         {"VHL": "high", "APOL1": "low", "PBRM1": "none"})
        self.assertIn("kidney-sparing", " ".join(summary["case_implications"]))  # RCC history from fhir.read_patient
        self.assertEqual(summary["not_reported"][0]["gene"], "BAP1")
        self.assertIn("ACMG2015", {c.source for c in dd.citations})
        panel_ref = dd.evidence[0].ref.removeprefix("cortex:evidence/")
        ev = self.cortex.get_evidence(3, "pt_marco", panel_ref)
        self.assertEqual(ev["kind"], "genomic_panel")
        self.assertEqual(self.cortex.read_blob(3, "pt_marco", ev["blob_sha256"]),
                         (self.root / "pt_marco" / "panel.json").read_bytes())

    async def test_non_genomics_request_is_ignored(self):
        await self.request({"items": [{"kind": "lab", "code": "egfr"}], "reason": "x"})
        self.assertEqual(self.delivered(), [])
        self.assertEqual(self.cortex.list_evidence(3, "pt_marco"), [])

    async def test_without_rcc_history_no_kidney_sparing_note(self):
        summaries = genomics_tools(FolderPanelSource(self.root))  # no conditions known
        self.rt.tools.register("fhir.read_patient", summaries["fhir.read_patient"])
        await self.request()
        summary = DataDelivered.model_validate(self.delivered()[0].payload).items[0]
        self.assertNotIn("kidney-sparing", " ".join(summary["case_implications"]))

    async def test_redelivery_is_processed_once(self):
        env = await self.request()
        await self.rt._on_message(env)  # the bus redelivers the same envelope to the seat
        await self.desk.bus.drain()
        self.assertEqual(len(self.delivered()), 1)
        self.assertEqual(len(self.cortex.list_evidence(3, "pt_marco")), 1)

    async def test_wrong_patient_panel_is_blocked(self):
        (self.root / "pt_anna").mkdir()
        (self.root / "pt_anna" / "panel.json").write_text(DEMO_PANEL.read_text())  # says marco-r inside
        await self.request(patient="pt_anna")
        self.assertIn("different patient", self.blocked()[0].detail["error"])
        self.assertEqual(self.delivered(), [])
        self.assertEqual(self.cortex.list_evidence(3, "pt_anna"), [])

    async def test_policy(self):
        ctx = self.rt.context()
        for tool, refusal in (("genomics.write_classification", "no_variant_reclassification"),
                              ("channel.send_message", "no_patient_contact")):
            with self.assertRaises(PolicyViolation) as cm:
                ctx.pep.check_tool(tool)
            self.assertEqual(cm.exception.refusal, refusal)
        with self.assertRaises(PolicyViolation) as cm:
            ctx.guard_content("genomic_result")
        self.assertEqual(cm.exception.refusal, "no_result_disclosure")

    async def test_reclassification_guard(self):
        import kcd.seats.genomics_interpreter as mod
        real = mod.interpret

        def tampering(panel, rules, context=None):
            out = real(panel, rules, context)
            out["findings"][0]["lab_classification"] = "benign"
            return out

        mod.interpret = tampering
        try:
            await self.request()
        finally:
            mod.interpret = real
        self.assertIn("VHL classification differs from the laboratory", self.blocked()[0].detail["error"])
        self.assertEqual(self.delivered(), [])

    def test_history_from_summary(self):
        self.assertEqual(history_from_summary({"conditions": ["RCC, post nephrectomy"]}), {"rcc": True})
        self.assertEqual(history_from_summary({"conditions": [{"display": "Clear-cell carcinoma"}]}), {"rcc": True})
        self.assertEqual(history_from_summary({"conditions": ["CKD G3a"]}), {"rcc": False})
        self.assertEqual(history_from_summary(None), {"rcc": False})


if __name__ == "__main__":
    unittest.main()
