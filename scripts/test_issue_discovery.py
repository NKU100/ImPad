import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.ci.codex_task import claim, list_issues, issue_body
from scripts.test_codex_task import candidate_report


def page(numbers, more=False):
    return {'data': {'repository': {'issues': {
        'nodes': [{'number': number} for number in numbers],
        'pageInfo': {'hasNextPage': more, 'endCursor': 'cursor' if more else None}}}}}


class IssueDiscoveryTest(unittest.TestCase):
    def test_nonempty_rest_response_needs_no_fallback(self):
        with patch('scripts.ci.codex_task.gh', return_value='[[{"number":29}]]') as github:
            self.assertEqual([{'number': 29}], list_issues('NKU100/ImPad'))
        self.assertEqual(1, github.call_count)

    def test_empty_rest_recovers_all_graphql_pages_using_original_records(self):
        records = {number: {'number': number, 'state': state, 'user': {'login': 'github-actions[bot]', 'type': 'Bot'}, 'labels': [], 'body': None}
                   for number, state in [(22, 'closed'), (29, 'open')]}
        def github(*args):
            if 'issues?state=all' in str(args):
                return '[[]]'
            if 'graphql' in args:
                self.assertIn('--paginate', args)
                return json.dumps([page([22], True), page([29])])
            return json.dumps(records[int(args[1].rsplit('/', 1)[1])])
        with patch('scripts.ci.codex_task.gh', side_effect=github):
            self.assertEqual(list(records.values()), list_issues('NKU100/ImPad'))

    def test_absence_requires_successful_empty_graphql_response(self):
        with patch('scripts.ci.codex_task.gh', side_effect=['[[]]', json.dumps([page([])])]):
            self.assertEqual([], list_issues('NKU100/ImPad'))

    def test_failed_or_incomplete_crosscheck_is_not_treated_as_no_issues(self):
        for response in [[], [{'errors': [{'message': 'unavailable'}]}], [page([29], True)],
                         [{'data': {'repository': None}}]]:
            with self.subTest(response=response), patch('scripts.ci.codex_task.gh', side_effect=['[[]]', json.dumps(response)]):
                with self.assertRaises(ValueError):
                    list_issues('NKU100/ImPad')

    def test_incomplete_issue_details_abort_discovery(self):
        complete = {'number': 29, 'state': 'open', 'user': {'login': 'bot', 'type': 'Bot'},
                    'labels': [], 'body': None}
        invalid = [{'number': 29}]
        invalid += [{k: v for k, v in complete.items() if k != field}
                    for field in ('state', 'user', 'labels', 'body')]
        invalid += [dict(complete, **override) for override in
                    ({'state': 'unknown'}, {'user': {}}, {'labels': [{}]}, {'body': 42},
                     {'number': 30}, {'pull_request': {}})]
        for detail in invalid:
            with self.subTest(detail=detail), patch('scripts.ci.codex_task.gh',
                    side_effect=['[[]]', json.dumps([page([29])]), json.dumps(detail)]):
                with self.assertRaises(ValueError):
                    list_issues('NKU100/ImPad')

    def test_claim_resumes_existing_issue_instead_of_creating_orphan(self):
        report = candidate_report()
        issue = {'number': 29, 'state': 'open', 'labels': [], 'user': {'login': 'github-actions[bot]', 'type': 'Bot'},
                 'body': issue_body(report, 'INTERRUPTED', 'https://github.com/NKU100/ImPad/actions/runs/1234')}
        def github(*args):
            if 'issues?state=all' in str(args):
                return '[[]]'
            if 'graphql' in args:
                return json.dumps([page([29])])
            if args == ('api', 'repos/NKU100/ImPad/issues/29'):
                return json.dumps(issue)
            return ''
        with tempfile.TemporaryDirectory() as root, \
             patch('scripts.ci.codex_task.gh', side_effect=github), \
             patch('scripts.ci.codex_task.eligible_candidate', return_value=True), \
             patch('scripts.ci.codex_task.read_owner_comments', return_value=[]), \
             patch('scripts.ci.codex_task.write_issue'), \
             patch('scripts.ci.codex_task.orphan_checkpoint') as orphan, \
             patch.dict(os.environ, {'GITHUB_OUTPUT': str(Path(root) / 'output')}):
            claim('NKU100/ImPad', report, [], Path(root), 'https://github.com/NKU100/ImPad/actions/runs/5678')
            output = (Path(root) / 'output').read_text()
            self.assertIn('claimed=true', output)
            self.assertIn('issue=29', output)
            orphan.assert_not_called()
