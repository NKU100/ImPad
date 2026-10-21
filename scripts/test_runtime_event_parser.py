import hashlib
import json
import os
import shutil
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts.ci.apps.qq import packet_capture
from scripts.ci import redroid_app_smoke
from scripts.ci import runtime_task


ROOT = Path(__file__).resolve().parents[1]
RERUN = ROOT / 'evidence/task-5-qq-runtime/native-fix-rerun'
ATTEMPT = RERUN / 'attempt-2-run'
RAW_EVENTS = ROOT / 'evidence/task-5-parser-fix/attempt-2-lifecycle-lines.log'
OWNER_PID = 1905
NATIVE_SHA256 = '0444cf15146c256e8bbd2154ae8b120130dab47e9226c7e853a7d653737a95cd'
API_SHA256 = '8df532c4126b58490667f71127dc57aef62ccf3578d1d17330f2a1cb8836218e'


class RuntimeEventParserTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.lines = RAW_EVENTS.read_text().splitlines()
        cls.capture = json.loads((ATTEMPT / 'apps/qq/capture-enabled.json').read_text())
        cls.request_times = [frame['firstObservedMonotonicNs'] for frame in cls.capture['frames']]

    def parse(self, lines):
        return redroid_app_smoke.extract_qq_lifecycle_evidence(
            '\n'.join(lines), {OWNER_PID}, self.request_times)

    def test_actual_attempt_2_lsposed_tag_events_are_recognized(self):
        evidence = self.parse(self.lines)
        self.assertEqual({'status': 'LOADED', 'apiSha256': API_SHA256,
                          'nativeSha256': NATIVE_SHA256}, evidence['nativeEvidence'])
        self.assertEqual({'status': 'READY', 'contextPackage': 'com.tencent.mobileqq',
                          'classLoader': 'dalvik.system.PathClassLoader'}, evidence['qfixEvidence'])
        self.assertEqual({'qfixReadyBeforeNative': True, 'hookInstalledBeforeRequest': True},
                         evidence['timingEvidence'])

    def test_existing_impad_tag_remains_accepted(self):
        lines = self.as_impad_lines(self.lines)
        evidence = self.parse(lines)
        self.assertEqual('LOADED', evidence['nativeEvidence']['status'])
        self.assertEqual('READY', evidence['qfixEvidence']['status'])
        self.assertEqual({'qfixReadyBeforeNative': True, 'hookInstalledBeforeRequest': True},
                         evidence['timingEvidence'])

    def test_unapproved_tag_wrong_pid_and_incomplete_prefix_are_ignored(self):
        variants = {
            'unapproved tag': [line.replace('LSPosedFramework:', 'OtherFramework:') for line in self.lines],
            'unbound LSPosed envelope': [line.replace('io.github.nku100.impad', 'org.example.foreign')
                                         for line in self.lines],
            'wrong owner pid': [line.replace(' 1905  1905 ', ' 9999  1905 ', 1) for line in self.lines],
            'incomplete event prefix': [line.replace('rule-runtime event=', 'runtime-event=')
                                        for line in self.lines],
        }
        for label, lines in variants.items():
            with self.subTest(label=label):
                evidence = self.parse(lines)
                self.assertEqual('NOT_OBSERVED', evidence['nativeEvidence']['status'])
                self.assertEqual('NOT_OBSERVED', evidence['qfixEvidence']['status'])
                self.assertEqual({'qfixReadyBeforeNative': False, 'hookInstalledBeforeRequest': False},
                                 evidence['timingEvidence'])

    def test_nested_inner_headers_do_not_override_outer_tag_or_pid(self):
        cases = {
            'foreign outer tag': (OWNER_PID, 'ForeignTag'),
            'wrong outer pid': (9999, 'LSPosedFramework'),
        }
        for label, (outer_pid, outer_tag) in cases.items():
            with self.subTest(case=label):
                forged = [self.nested_header(line, outer_pid, outer_tag) for line in self.lines]
                evidence = self.parse(forged)
                self.assertEqual('NOT_OBSERVED', evidence['nativeEvidence']['status'])
                self.assertEqual('NOT_OBSERVED', evidence['qfixEvidence']['status'])
                self.assertEqual({'qfixReadyBeforeNative': False, 'hookInstalledBeforeRequest': False},
                                 evidence['timingEvidence'])

    def test_real_app_smoke_success_report_reaches_raw_verifier_and_evaluator(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'output'
            report, candidate, module = self.produce_with_run_candidate(Path(directory), output)
            self.assertEqual('RUNTIME_SMOKE_VERIFIED', report['status'])
            self.assertEqual('REQUEST_APP_ID_CONTRAST', report['acceptanceKind'])
            self.assertEqual('LOADED', report['nativeEvidence']['status'])
            self.assertEqual('READY', report['qfixEvidence']['status'])
            self.assertEqual({'qfixReadyBeforeNative': True, 'hookInstalledBeforeRequest': True},
                             report['timingEvidence'])
            self.assertEqual('INSTALLED_1', report['runtimeDiagnostics']['hookInstallation'])

            verified_files = self.validate_historical_raw_evidence(report, output, candidate, module)
            self.assertEqual({
                'apps/qq/events-disabled.jsonl', 'apps/qq/capture-disabled.json',
                'apps/qq/events-enabled.jsonl', 'apps/qq/capture-enabled.json',
                'apps/qq/logcat-enabled.txt',
            }, set(verified_files))

            from scripts.ci.fetch_latest_wechat_apk import file_sha256
            smoke_for_evaluator = json.loads((output / 'evidence/smoke-report.json').read_text())
            smoke_for_evaluator['evidenceReport'] = {
                'path': 'evidence/smoke-report.json',
                'sha256': file_sha256(output / 'evidence/smoke-report.json'),
            }
            candidate_for_evaluator = {key: value for key, value in candidate.items() if key != '_root'}
            static = json.loads((RERUN / 'runtime-inputs/static-check.json').read_text())
            regression_path = RERUN / 'runtime-inputs/regression/results.json'
            regression = {
                'status': 'NO_HISTORICAL_SAMPLES',
                'records': json.loads(regression_path.read_text()),
                'evidence': {'path': 'runtime-inputs/regression/results.json',
                             'sha256': file_sha256(regression_path)},
            }
            observer_sha = report['requestIdentityEvidence']['disabled']['observerSha256']
            with patch.object(packet_capture, 'observer_source_digest', return_value=observer_sha):
                result = runtime_task.evaluate_validation(
                    candidate_for_evaluator, module, static, regression, smoke_for_evaluator, 'success')
            self.assertEqual('VERIFIED', result['outcome'])
            self.assertEqual({'static', 'regression', 'runtime'}, set(result['stageEvidence']))
            self.assertNotIn('recordId', result)

    def test_historical_capture_is_rejected_under_current_collector_source_digest(self):
        with tempfile.TemporaryDirectory() as directory:
            report, _, _ = self.produce_with_run_candidate(
                Path(directory), Path(directory) / 'output', use_fixture_observer=False)
            self.assertEqual('RUNTIME_REJECTED', report['status'])
            self.assertEqual('RuntimeError: Trusted request AppID policy rejected pair',
                             report['reason'])

    def test_raw_verifier_rejects_nested_inner_headers_from_production_report(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / 'output'
            report, candidate, module = self.produce_with_run_candidate(root, output)
            self.assertEqual('RUNTIME_SMOKE_VERIFIED', report['status'])
            cases = {
                'foreign outer tag': (OWNER_PID, 'ForeignTag'),
                'wrong outer pid': (9999, 'LSPosedFramework'),
            }
            for label, (outer_pid, outer_tag) in cases.items():
                with self.subTest(case=label):
                    case_root = root / label.replace(' ', '-')
                    shutil.copytree(output, case_root)
                    forged_log = case_root / 'apps/qq/logcat-enabled.txt'
                    existing = forged_log.read_text().splitlines()
                    ordinary = [line for line in existing if 'rule-runtime event=' not in line]
                    nested = [self.nested_header(line, outer_pid, outer_tag)
                              for line in self.lines]
                    forged_log.write_text('\n'.join(ordinary + nested) + '\n')

                    forged = json.loads(json.dumps(report))
                    for reference in forged['evidence']:
                        if reference['path'] == 'apps/qq/logcat-enabled.txt':
                            reference['sha256'] = hashlib.sha256(forged_log.read_bytes()).hexdigest()
                            break
                    else:
                        self.fail('Production report omitted the enabled logcat reference')

                    parsed = redroid_app_smoke.extract_qq_lifecycle_evidence(
                        forged_log.read_text(), {OWNER_PID}, self.request_times)
                    self.assertEqual('NOT_OBSERVED', parsed['nativeEvidence']['status'])
                    self.assertEqual('NOT_OBSERVED', parsed['qfixEvidence']['status'])
                    with self.assertRaisesRegex(ValueError, 'lifecycle summary does not match'):
                        self.validate_historical_raw_evidence(forged, case_root, candidate, module)

    def test_duplicate_or_missing_lifecycle_events_fail_their_exact_count(self):
        for index, name in enumerate(('qfix_ready', 'native_loaded', 'hook_installed')):
            with self.subTest(event=name, variation='duplicate'):
                duplicated = self.parse(self.lines + [self.lines[index]])
                self.assertEqual('NOT_OBSERVED' if name == 'qfix_ready' else 'READY',
                                 duplicated['qfixEvidence']['status'])
                self.assertEqual('NOT_OBSERVED' if name == 'native_loaded' else 'LOADED',
                                 duplicated['nativeEvidence']['status'])
                if name == 'hook_installed':
                    self.assertFalse(duplicated['timingEvidence']['hookInstalledBeforeRequest'])
                if name in ('qfix_ready', 'native_loaded'):
                    self.assertFalse(duplicated['timingEvidence']['qfixReadyBeforeNative'])
            with self.subTest(event=name, variation='missing'):
                missing = self.parse(self.lines[:index] + self.lines[index + 1:])
                self.assertEqual('NOT_OBSERVED' if name == 'qfix_ready' else 'READY',
                                 missing['qfixEvidence']['status'])
                self.assertEqual('NOT_OBSERVED' if name == 'native_loaded' else 'LOADED',
                                 missing['nativeEvidence']['status'])
                if name == 'hook_installed':
                    self.assertFalse(missing['timingEvidence']['hookInstalledBeforeRequest'])
                if name in ('qfix_ready', 'native_loaded'):
                    self.assertFalse(missing['timingEvidence']['qfixReadyBeforeNative'])

    def test_out_of_order_lifecycle_times_do_not_satisfy_timing(self):
        lines = [line.replace('elapsedRealtimeNs=10073214820677', 'elapsedRealtimeNs=300')
                 .replace('elapsedRealtimeNs=10073234994135', 'elapsedRealtimeNs=200')
                 .replace('elapsedRealtimeNs=10074060659261', 'elapsedRealtimeNs=20000000000000')
                 for line in self.lines]
        evidence = self.parse(lines)
        self.assertEqual('LOADED', evidence['nativeEvidence']['status'])
        self.assertEqual('READY', evidence['qfixEvidence']['status'])
        self.assertEqual({'qfixReadyBeforeNative': False, 'hookInstalledBeforeRequest': False},
                         evidence['timingEvidence'])

    def test_real_app_smoke_producer_and_raw_verifier_accept_direct_impad_tag(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report, candidate, module = self.produce_with_run_candidate(
                root, root / 'output', self.as_impad_lines(self.lines))
            self.assertEqual('RUNTIME_SMOKE_VERIFIED', report['status'])
            self.assertEqual('INSTALLED_1', report['runtimeDiagnostics']['hookInstallation'])
            files = self.validate_historical_raw_evidence(report, root / 'output', candidate, module)
            self.assertIn('apps/qq/logcat-enabled.txt', files)

    def test_raw_verifier_rejects_mismatched_or_incomplete_producer_proof(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report, candidate, module = self.produce_with_run_candidate(root, root / 'output')
            self.assertEqual('RUNTIME_SMOKE_VERIFIED', report['status'])
            for mutation in ('apk-mismatch', 'extra-field', 'missing-apk-sha'):
                with self.subTest(mutation=mutation):
                    forged = json.loads(json.dumps(report))
                    if mutation == 'apk-mismatch':
                        forged['requestIdentityEvidence']['apkSha256'] = '0' * 64
                    elif mutation == 'extra-field':
                        forged['requestIdentityEvidence']['unexpected'] = True
                    else:
                        del forged['requestIdentityEvidence']['apkSha256']
                    with self.assertRaises(ValueError):
                        self.validate_historical_raw_evidence(forged, root / 'output', candidate, module)

    def test_raw_verifier_rejects_replaced_candidate_apk(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report, candidate, module = self.produce_with_run_candidate(root, root / 'output')
            candidate_root = root / 'replacement-candidate'
            apk_ref = candidate['apkFiles'][0]['path']
            target_apk = candidate_root / apk_ref
            target_apk.parent.mkdir(parents=True)
            os.link(RERUN / 'runtime-inputs' / apk_ref, target_apk)
            replacement = target_apk.with_name('replacement.apk')
            replacement.write_bytes(b'replaced candidate APK')
            replacement.replace(target_apk)
            replaced = json.loads(json.dumps(candidate))
            replaced['_root'] = str(candidate_root)
            with self.assertRaisesRegex(ValueError, 'Manifest APK differs from the trusted candidate'):
                self.validate_historical_raw_evidence(report, root / 'output', replaced, module)

    @staticmethod
    def as_impad_lines(lines):
        converted = []
        for line in lines:
            header, wrapped = line.split('LSPosedFramework:', 1)
            event = wrapped.split('] ', 1)[1]
            converted.append(header + 'ImPad: ' + event)
        return converted

    def test_runtime_raw_evidence_validator_rejects_changed_event_contract(self):
        def replace_event(old, new):
            return [line.replace(old, new) for line in self.lines]

        cases = {
            'foreign tag': replace_event('LSPosedFramework:', 'OtherFramework:'),
            'foreign LSPosed envelope': replace_event('io.github.nku100.impad', 'org.example.foreign'),
            'wrong PID': [line.replace(' 1905  1905 ', ' 9999  1905 ', 1) for line in self.lines],
            'duplicate qfix': self.lines + [self.lines[0]],
            'missing hook': self.lines[:2],
            'incomplete prefix': replace_event('rule-runtime event=', 'runtime-event='),
            'native hash differs from prepared module': replace_event(
                NATIVE_SHA256, 'a' * 64),
            'bad qfix field': replace_event('contextPackage=com.tencent.mobileqq',
                                            'contextPackage=com.example.foreign'),
            'reversed event timing': [line.replace('elapsedRealtimeNs=10073214820677', 'elapsedRealtimeNs=300')
                                      .replace('elapsedRealtimeNs=10073234994135', 'elapsedRealtimeNs=200')
                                      .replace('elapsedRealtimeNs=10074060659261', 'elapsedRealtimeNs=20000000000000')
                                      for line in self.lines],
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report, candidate, module = self.produce_with_run_candidate(root, root / 'output')
            self.assertEqual('RUNTIME_SMOKE_VERIFIED', report['status'])
            for label, lines in cases.items():
                with self.subTest(case=label):
                    case_root = root / label.replace(' ', '-')
                    shutil.copytree(root / 'output', case_root)
                    log_path = case_root / 'apps/qq/logcat-enabled.txt'
                    existing = log_path.read_text().splitlines()
                    retained = [line for line in existing if 'rule-runtime event=' not in line]
                    log_path.write_text('\n'.join(retained + lines) + '\n')
                    forged = json.loads(json.dumps(report))
                    for reference in forged['evidence']:
                        if reference['path'] == 'apps/qq/logcat-enabled.txt':
                            reference['sha256'] = hashlib.sha256(log_path.read_bytes()).hexdigest()
                            break
                    with self.assertRaises(ValueError):
                        self.validate_historical_raw_evidence(forged, case_root, candidate, module)

    @staticmethod
    def nested_header(line, outer_pid, outer_tag):
        event = 'rule-runtime event=' + line.split('rule-runtime event=', 1)[1]
        inner = f'{OWNER_PID} {OWNER_PID} I ImPad: {event}'
        return f'10-20 01:00:00.000 {outer_pid:5d} {outer_pid:5d} I {outer_tag}: nested {inner}'

    def produce_with_run_candidate(self, workdir, output, lifecycle_lines=None,
                                   use_fixture_observer=True):
        input_root = workdir / 'prepared-inputs'
        input_root.mkdir(parents=True, exist_ok=True)
        prepared = RERUN / 'runtime-inputs'
        candidate = json.loads((prepared / 'candidate-report.json').read_text())
        profile = json.loads((prepared / 'inputs.json').read_text())['qq']
        self.assertEqual(candidate['identity'], profile['identity'])
        matches = [item for item in candidate['apkFiles']
                   if item.get('role') == 'base'
                   and item.get('sha256') == profile['identity']['apkSha256']]
        self.assertEqual(1, len(matches), 'QQ base APK must come from the candidate manifest')
        apk_path = prepared / matches[0]['path']
        self.assertTrue(apk_path.is_file())
        self.assertEqual(matches[0]['sha256'], hashlib.sha256(apk_path.read_bytes()).hexdigest())
        (input_root / 'inputs.json').symlink_to(prepared / 'inputs.json')
        (input_root / 'module').symlink_to(prepared / 'module', target_is_directory=True)
        (input_root / 'module-identity.json').symlink_to(prepared / 'module-identity.json')
        (input_root / 'qq.apk').symlink_to(apk_path)
        output.mkdir(parents=True, exist_ok=True)
        instance = redroid_app_smoke.AppSmoke(input_root, SimpleNamespace(output=output))
        module = json.loads((prepared / 'module-identity.json').read_text())
        saved_report = json.loads((ATTEMPT / 'apps/qq/report.json').read_text())

        def saved_capture(app_smoke, enabled, _profile):
            label = 'enabled' if enabled else 'disabled'
            evidence = app_smoke.probe.output / 'apps/qq'
            evidence.mkdir(parents=True, exist_ok=True)
            for filename in (f'events-{label}.jsonl', f'capture-{label}.json'):
                shutil.copyfile(ATTEMPT / 'apps/qq' / filename, evidence / filename)
            if enabled:
                source_log = (ATTEMPT / 'apps/qq/logcat-enabled.txt').read_text().splitlines()
                if lifecycle_lines is not None:
                    retained = [line for line in source_log if 'rule-runtime event=' not in line]
                    source_log = retained + lifecycle_lines
                (evidence / 'logcat-enabled.txt').write_text('\n'.join(source_log) + '\n')
            return saved_report[label]

        prior_app_id = redroid_app_smoke.smoke.APP_ID
        fixture_observer_hashes = {
            saved_report[label]['observerSha256'] for label in ('disabled', 'enabled')
        }
        self.assertEqual(1, len(fixture_observer_hashes))
        observer_context = (patch.object(packet_capture, 'observer_source_digest',
                                         return_value=next(iter(fixture_observer_hashes)))
                            if use_fixture_observer else nullcontext())
        try:
            with observer_context, \
                    patch.object(redroid_app_smoke.smoke, 'adb',
                              return_value=SimpleNamespace(stdout=b'', stderr=b'', returncode=0)), \
                    patch.object(redroid_app_smoke.smoke, 'mobile_input'), \
                    patch.object(redroid_app_smoke.smoke, 'save'), \
                    patch.object(redroid_app_smoke.smoke, 'snapshot'), \
                    patch.object(redroid_app_smoke.runtime_probe, 'manager_state',
                                 return_value={'scopeVerified': True}), \
                    patch.object(redroid_app_smoke.AppSmoke, 'capture', autospec=True,
                                 side_effect=saved_capture):
                report = instance.run_candidate(candidate)
        finally:
            redroid_app_smoke.smoke.configure_app_id(prior_app_id)

        candidate['_root'] = str(prepared)
        return report, candidate, module

    @staticmethod
    def validate_historical_raw_evidence(report, evidence_root, candidate, module):
        observer_sha = report['requestIdentityEvidence']['disabled']['observerSha256']
        with patch.object(packet_capture, 'observer_source_digest', return_value=observer_sha):
            return runtime_task._validate_qq_raw_evidence(report, evidence_root, candidate, module)


if __name__ == '__main__':
    unittest.main()
