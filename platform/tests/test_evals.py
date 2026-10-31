import tempfile
import unittest
from pathlib import Path

from evals import dataset, metrics, rules_baseline, runner, targets
from evals.cli import main


class MetricsTest(unittest.TestCase):
    def test_per_class_and_binary(self):
        pc = metrics.per_class(["a", "a", "b"], ["a", "b", "b"], ["a", "b"])
        self.assertAlmostEqual(pc["a"]["precision"], 1.0)
        self.assertAlmostEqual(pc["a"]["recall"], 0.5)
        self.assertAlmostEqual(pc["b"]["precision"], 0.5)
        b = metrics.binary([True, True, False], [True, False, True])
        self.assertEqual((b["tp"], b["fp"], b["fn"]), (1, 1, 1))

    def test_auroc(self):
        self.assertEqual(metrics.auroc([True, False], [0.9, 0.1]), 1.0)
        self.assertEqual(metrics.auroc([True, False], [0.5, 0.5]), 0.5)
        self.assertIsNone(metrics.auroc([True, True], [0.2, 0.3]))

    def test_ece(self):
        self.assertAlmostEqual(metrics.ece([True, False], [1.0, 0.0]), 0.0)
        self.assertAlmostEqual(metrics.ece([False, False], [1.0, 1.0]), 1.0)


class RulesBaselineTest(unittest.TestCase):
    """The port must behave like the demo's nlu.mjs + km.mjs#redFlags."""

    def p(self, text, rcc=True):
        return rules_baseline.predict({"text": text, "context": {"history": {"rcc": rcc}}})["urgency"]

    def test_demo_golden_path(self):
        self.assertEqual(self.p("Ho dolore al fianco destro, direi 8, e le urine sono rosate da ieri."), "high")

    def test_core_without_systemic_or_history_is_medium(self):
        self.assertEqual(self.p("blood in the urine and flank pain 8/10", rcc=False), "medium")

    def test_single_symptom_and_nothing(self):
        self.assertEqual(self.p("blood in the urine"), "medium")
        self.assertEqual(self.p("When is my next CT scan?"), "none")


class DatasetTest(unittest.TestCase):
    def test_all_datasets_valid(self):
        for name in dataset.available():
            ds = dataset.load(name)
            self.assertEqual(ds.errors, [], name)

    def test_validation_catches_bad_label_and_duplicate(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "x").mkdir()
            (root / "x" / "card.json").write_text(
                '{"name":"x","version":"0","task":"red_flag","labels":["none","high"],"source":"t",'
                '"labeller":"t","review_status":"draft","created":"2026-10-17"}', encoding="utf-8")
            (root / "x" / "data.jsonl").write_text(
                '{"id":"1","text":"a","label":{"urgency":"maybe"}}\n{"id":"1","text":"b","label":{"urgency":"none"}}\n',
                encoding="utf-8")
            errors = dataset.load("x", root).errors
            self.assertTrue(any("not in card labels" in e for e in errors))
            self.assertTrue(any("duplicate id" in e for e in errors))


class RunnerTest(unittest.TestCase):
    def test_failing_item_is_a_miss_not_dropped(self):
        ds = dataset.load("red_flags")

        def broken(item):
            raise RuntimeError("model down")

        result = runner.run(ds, broken)
        self.assertEqual(result["metrics"]["n"], len(ds.items))
        self.assertEqual(result["metrics"]["errors"], len(ds.items))
        self.assertEqual(result["metrics"]["high"]["recall"], 0.0)

    def test_thresholds(self):
        ds = dataset.load("red_flags")
        result = runner.run(ds, targets.get("rules_baseline")[1])
        checks = runner.check_thresholds(result, {"high.recall": 0.0, "does.not.exist": 0.5})
        self.assertTrue(checks[0]["ok"])
        self.assertFalse(checks[1]["ok"])


class CliTest(unittest.TestCase):
    def test_run_writes_report_and_gate_fails_on_baseline(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(main(["run", "red_flags", "--target", "rules_baseline", "--out", d]), 0)
            self.assertTrue(list(Path(d, "red_flags").glob("rules_baseline-*.md")))
            self.assertEqual(main(["run", "red_flags", "--target", "rules_baseline", "--gate", "--out", d]), 1)

    def test_classification_task_genomics(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(main(["run", "genomics_relevance", "--target", "genomics_rules", "--gate", "--out", d]), 0)
            report = next(Path(d, "genomics_relevance").glob("genomics_rules-*.md")).read_text(encoding="utf-8")
            self.assertIn("Macro F1", report)
            self.assertNotIn("Must-escalate", report)
        self.assertEqual(main(["run", "genomics_relevance", "--target", "rules_baseline"]), 1)  # task mismatch

    def test_edge_llm_unavailable_without_config(self):
        import os
        if os.environ.get("EDGE_LLM_BASE_URL"):
            self.skipTest("endpoint configured")
        self.assertEqual(main(["run", "red_flags", "--target", "edge_llm", "--out", tempfile.gettempdir()]), 2)


if __name__ == "__main__":
    unittest.main()
