import contextlib
import io
import unittest

from kcd.demo import main


class GoldenPathTest(unittest.TestCase):
    """The whole desk on kcd_core: deck slide 28, steps 1 to 8, with Marco R."""

    def test_demo_runs_end_to_end(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main([])
        text = out.getvalue()
        self.assertEqual(code, 0, text[-2000:])
        for expected in ('"paged": "dr_rossi"', "every citation exists in the Knowledge Model: ok",
                         '"verified": true', "VHL pathogenic -> high", '"genetic_disclosure"',
                         "Please stop ibuprofen", "avoid_drug_class:NSAID", "routing:rcc_red_flag",
                         '"dead_letters": []', '"core_audit_chain_ok": true', '"complete": true'):
            self.assertIn(expected, text)


if __name__ == "__main__":
    unittest.main()
