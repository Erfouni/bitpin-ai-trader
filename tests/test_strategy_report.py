import unittest
from bitpin.strategy_report import render_report


class StrategyReportTests(unittest.TestCase):
    def test_missing_evaluation_is_explicit_and_snapshot_escaped(self):
        page=render_report({'generated_at':100,'receipt_count':4,'sources':{},'packets':{},'status':'<script>'},{})
        self.assertIn('No historical result loaded',page)
        self.assertNotIn('<script></script>',page)
        self.assertIn('viewport',page)
        self.assertIn('Research only',page)
        self.assertIn('Fresh Kimi: not run',page)

    def test_data_script_cannot_break_out_of_json(self):
        page=render_report({'generated_at':100,'receipt_count':0,'sources':{},'packets':{}}, {'note':'</script><img src=x>'})
        self.assertNotIn('</script><img src=x>',page)
        self.assertIn('\\u003c/script',page)


if __name__=='__main__': unittest.main()
