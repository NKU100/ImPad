import tempfile
import unittest
from pathlib import Path
from scripts.ci.adaptation_summary import read_summary, render_result

class SummaryTest(unittest.TestCase):
    def test_terminal_summary_is_bounded_and_interruption_does_not_reuse_success(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'summary.json'
            path.write_text('{"completed":["构建通过"],"blockers":[],"nextActions":[],"questions":[]}')
            self.assertEqual(['构建通过'], read_summary(path)['completed'])
            text = render_result('企微', '5.0.12', 'interrupted', 'https://example.com/run', read_summary(path))
            self.assertIn('执行中断', text)
            self.assertNotIn('构建通过', text)

    def test_blocked_output_has_readable_actions_without_raw_json(self):
        text = render_result('企微', '5.0.12', 'blocked', 'https://example.com/run',
                             {'completed':['定位登录页'], 'blockers':['缺少输入'], 'nextActions':['恢复测试'], 'questions':['请确认入口']})
        self.assertIn('需要你操作', text)
        self.assertIn('缺少输入', text)
        self.assertNotIn('```json', text)

    def test_malformed_summary_falls_back_without_crashing(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'summary.json';path.write_text('{"completed":"wrong"}')
            self.assertEqual({}, read_summary(path))
