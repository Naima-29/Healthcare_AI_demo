import contextlib
import io
import unittest

from kcd.scenarios.edge_cases import main


class EdgeCaseScenarioTest(unittest.TestCase):
    """Every edge case for the synthetic patient Sofia B. passes through the whole desk (scripted CT model)."""

    def test_all_edge_cases_pass(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main([])
        text = out.getvalue()
        failed = [line for line in text.splitlines() if "[FAIL]" in line]
        self.assertEqual(failed, [], text)
        self.assertEqual(code, 0)
        self.assertIn("52 of 52 checks passed", text)


if __name__ == "__main__":
    unittest.main()
