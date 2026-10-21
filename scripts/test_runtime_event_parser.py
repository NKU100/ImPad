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

from scripts.ci.apps.qq import packet_capture, request_protocol
from scripts.ci import redroid_app_smoke
from scripts.ci import runtime_task
from scripts.testdata.runtime_replay import prepare_candidate_bundle, qq_packet


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / 'scripts/testdata/runtime-replay/qq'
OWNER_PID = 1905
NATIVE_SHA256 = '0444cf15146c256e8bbd2154ae8b120130dab47e9226c7e853a7d653737a95cd'
API_SHA256 = '8df532c4126b58490667f71127dc57aef62ccf3578d1d17330f2a1cb8836218e'


class RuntimeEventParserTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.lines = (FIXTURE / 'lifecycle.log').read_text().splitlines()
        cls.lifecycle_lines = [line for line in cls.lines if 'rule-runtime event=' in line]
        cls.observer_sha = json.loads((FIXTURE / 'frozen-observer.json').read_text())['observerSha256']
        cls.request_times = [1000]

    def parse(self, lines):
        return redroid_app_smoke.extract_qq_lifecycle_evidence(
            '\n'.join(lines), {OWNER_PID}, self.request_times)

    def test_pinned_sanitized_lsposed_lifecycle_fixture_is_recognized(self):
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
            'wrong owner pid': [line.replace(' 1905  1905 ', ' 9999  9999 ')
                                if 'rule-runtime event=' in line else line for line in self.lines],
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
                forged = [self.nested_header(line, outer_pid, outer_tag)
                          for line in self.lifecycle_lines]
                evidence = self.parse(forged)
                self.assertEqual('NOT_OBSERVED', evidence['nativeEvidence']['status'])
                self.assertEqual('NOT_OBSERVED', evidence['qfixEvidence']['status'])
                self.assertEqual({'qfixReadyBeforeNative': False, 'hookInstalledBeforeRequest': False},
                                 evidence['timingEvidence'])

    def test_production_smoke_report_reaches_raw_verifier_and_evaluator(self):
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
            static = json.loads((Path(candidate['_root']) / candidate['staticReport']).read_text())
            regression_path = Path(candidate['_root']) / 'regression/results.json'
            regression = {
                'status': 'NO_HISTORICAL_SAMPLES',
                'records': json.loads(regression_path.read_text()),
                'evidence': {'path': 'regression/results.json',
                             'sha256': file_sha256(regression_path)},
            }
            observer_sha = report['requestIdentityEvidence']['disabled']['observerSha256']
            with patch.object(packet_capture, 'observer_source_digest', return_value=observer_sha):
                result = runtime_task.evaluate_validation(
                    candidate_for_evaluator, module, static, regression, smoke_for_evaluator, 'success')
            self.assertEqual('VERIFIED', result['outcome'])
            self.assertEqual({'static', 'regression', 'runtime'}, set(result['stageEvidence']))
            self.assertNotIn('recordId', result)

    def test_frozen_observer_capture_is_rejected_under_current_collector_source_digest(self):
        with tempfile.TemporaryDirectory() as directory:
            report, _, _ = self.produce_with_run_candidate(
                Path(directory), Path(directory) / 'output', use_fixture_observer=False)
            self.assertEqual('RUNTIME_REJECTED', report['status'])
            self.assertEqual('RuntimeError: Trusted request AppID policy rejected pair',
                             report['reason'])

    def test_raw_verifier_rejects_nested_inner_headers_from_synthetic_report(self):
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
                              for line in self.lifecycle_lines]
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
            lifecycle = self.lifecycle_lines
            with self.subTest(event=name, variation='duplicate'):
                duplicated = self.parse(lifecycle + [lifecycle[index]])
                self.assertEqual('NOT_OBSERVED' if name == 'qfix_ready' else 'READY',
                                 duplicated['qfixEvidence']['status'])
                self.assertEqual('NOT_OBSERVED' if name == 'native_loaded' else 'LOADED',
                                 duplicated['nativeEvidence']['status'])
                if name == 'hook_installed':
                    self.assertFalse(duplicated['timingEvidence']['hookInstalledBeforeRequest'])
                if name in ('qfix_ready', 'native_loaded'):
                    self.assertFalse(duplicated['timingEvidence']['qfixReadyBeforeNative'])
            with self.subTest(event=name, variation='missing'):
                missing = self.parse(lifecycle[:index] + lifecycle[index + 1:])
                self.assertEqual('NOT_OBSERVED' if name == 'qfix_ready' else 'READY',
                                 missing['qfixEvidence']['status'])
                self.assertEqual('NOT_OBSERVED' if name == 'native_loaded' else 'LOADED',
                                 missing['nativeEvidence']['status'])
                if name == 'hook_installed':
                    self.assertFalse(missing['timingEvidence']['hookInstalledBeforeRequest'])
                if name in ('qfix_ready', 'native_loaded'):
                    self.assertFalse(missing['timingEvidence']['qfixReadyBeforeNative'])

    def test_out_of_order_lifecycle_times_do_not_satisfy_timing(self):
        lines = [line.replace('qfix_ready elapsedRealtimeNs=100 ',
                              'qfix_ready elapsedRealtimeNs=300 ')
                 .replace('hook_installed elapsedRealtimeNs=300 ',
                          'hook_installed elapsedRealtimeNs=20000000000000 ')
                 for line in self.lines]
        evidence = self.parse(lines)
        self.assertEqual('LOADED', evidence['nativeEvidence']['status'])
        self.assertEqual('READY', evidence['qfixEvidence']['status'])
        self.assertEqual({'qfixReadyBeforeNative': False, 'hookInstalledBeforeRequest': False},
                         evidence['timingEvidence'])

    def test_production_smoke_producer_and_raw_verifier_accept_direct_impad_tag(self):
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
            shutil.copyfile(Path(candidate['_root']) / apk_ref, target_apk)
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
            if 'LSPosedFramework:' not in line:
                converted.append(line)
                continue
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
            'duplicate qfix': self.lines + [self.lifecycle_lines[0]],
            'missing hook': self.lines[:2],
            'incomplete prefix': replace_event('rule-runtime event=', 'runtime-event='),
            'native hash differs from prepared module': replace_event(
                NATIVE_SHA256, 'a' * 64),
            'bad qfix field': replace_event('contextPackage=com.tencent.mobileqq',
                                            'contextPackage=com.example.foreign'),
            'reversed event timing': [line.replace('qfix_ready elapsedRealtimeNs=100 ',
                                                   'qfix_ready elapsedRealtimeNs=300 ')
                                      .replace('hook_installed elapsedRealtimeNs=300 ',
                                               'hook_installed elapsedRealtimeNs=20000000000000 ')
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
        if 'rule-runtime event=' not in line:
            raise ValueError('Expected lifecycle event line')
        event = 'rule-runtime event=' + line.split('rule-runtime event=', 1)[1]
        inner = f'{OWNER_PID} {OWNER_PID} I ImPad: {event}'
        return f'10-20 01:00:00.000 {outer_pid:5d} {outer_pid:5d} I {outer_tag}: nested {inner}'

    def produce_with_run_candidate(self, workdir, output, lifecycle_lines=None,
                                   use_fixture_observer=True):
        candidate_root, candidate_path, candidate, _, module_apk = prepare_candidate_bundle('qq', workdir)
        candidate['_root'] = str(candidate_root)
        module = candidate['module']
        apk_path = candidate_root / candidate['apkFiles'][0]['path']
        self.assertEqual(candidate['identity']['apkSha256'], hashlib.sha256(apk_path.read_bytes()).hexdigest())
        observer = self.observer_sha
        app_root = workdir / 'smoke-inputs'
        (app_root / 'module').mkdir(parents=True)
        shutil.copyfile(module_apk, app_root / 'module/app-debug.apk')
        module_path = app_root / 'module-identity.json'
        module_path.write_text(json.dumps(module, indent=2) + '\n')
        shutil.copyfile(apk_path, app_root / 'qq.apk')
        (app_root / 'inputs.json').write_text(json.dumps({'qq': {
            'identity': candidate['identity'], 'signerVerified': True,
            'candidateReport': candidate, 'moduleIdentity': module,
            'requestIdentityExpectation': candidate['requestIdentityExpectation'],
        }}, indent=2) + '\n')
        acceptance_path = workdir / 'compatibility/qq/acceptance.json'
        acceptance_path.parent.mkdir(parents=True)
        acceptance_path.write_text(json.dumps({'schemaVersion': 1, 'appId': 'qq', 'records': []}) + '\n')
        regression_path = candidate_root / 'regression/results.json'
        regression_path.parent.mkdir(parents=True)
        regression_path.write_text(json.dumps([{
            'appId': 'qq', 'status': 'NO_HISTORICAL_SAMPLES',
            'acceptanceSnapshotSha256': hashlib.sha256(acceptance_path.read_bytes()).hexdigest(),
            'initialMigration': True,
        }]) + '\n')
        output.mkdir(parents=True, exist_ok=True)
        instance = redroid_app_smoke.AppSmoke(app_root, SimpleNamespace(output=output))

        def capture_events(enabled, profile):
            label = 'enabled' if enabled else 'disabled'
            evidence = output / 'apps/qq'
            evidence.mkdir(parents=True, exist_ok=True)
            uid, pid = 10089, OWNER_PID
            app_id = 20 if enabled else 10
            request_time = 1000 if enabled else 900
            events = [
                {'kind': 'ready', 'uid': uid},
                {'kind': 'segment', 'pid': pid, 'uid': uid, 'started': 999432, 'inode': 355315,
                 'source': '0100007f', 'destination': '0200007f', 'sourcePort': 42000,
                 'destinationPort': 443, 'sequence': 100, 'flags': 2, 'generation': 1,
                 'payload': '', 'observedMonotonicNs': None},
                {'kind': 'segment', 'pid': pid, 'uid': uid, 'started': 999432, 'inode': 355315,
                 'source': '0100007f', 'destination': '0200007f', 'sourcePort': 42000,
                 'destinationPort': 443, 'sequence': 101, 'flags': 16, 'generation': 1,
                 'payload': qq_packet(app_id, 86103 if not enabled else 86104,
                                      sorted(request_protocol.SECURITY_COMMANDS)[0]).hex(),
                 'observedMonotonicNs': request_time},
                {'kind': 'end', 'kernelDrops': 0},
            ]
            capture = packet_capture.TcpCapture(uid)
            for event in events:
                capture.feed(event)
            raw = capture.result()
            self.assertTrue(raw['captureHealthy'], raw)
            (evidence / f'events-{label}.jsonl').write_text(
                ''.join(json.dumps(event, separators=(',', ':')) + '\n' for event in events))
            (evidence / f'capture-{label}.json').write_text(json.dumps(raw, indent=2) + '\n')
            report = dict(raw, moduleEnabled=enabled, scopeVerified=True,
                          observerSha256=observer, collectorBinarySha256='c' * 64,
                          ndkVersion='29.0.14206865', apkSha256=profile['identity']['apkSha256'],
                          appUid=uid, moduleApkSha256=instance.module_hash,
                          processName='com.tencent.mobileqq:MSF')
            if enabled:
                rows = (FIXTURE / 'lifecycle.log').read_text().splitlines()
                if lifecycle_lines is not None:
                    rows = [line for line in rows if 'rule-runtime event=' not in line] + lifecycle_lines
                (evidence / 'logcat-enabled.txt').write_text('\n'.join(rows) + '\n')
            return report

        prior_app_id = redroid_app_smoke.smoke.APP_ID
        observer_context = (patch.object(packet_capture, 'observer_source_digest', return_value=observer)
                            if use_fixture_observer else nullcontext())

        def save_log(name, _result):
            if name == 'logcat-enabled.txt':
                return (output / 'apps/qq/logcat-enabled.txt').read_bytes()
            return b''

        try:
            with observer_context, \
                    patch.object(redroid_app_smoke.smoke, 'adb',
                              return_value=SimpleNamespace(stdout=b'', stderr=b'', returncode=0)), \
                    patch.object(redroid_app_smoke.smoke, 'mobile_input'), \
                    patch.object(redroid_app_smoke.smoke, 'save', side_effect=save_log), \
                    patch.object(redroid_app_smoke.smoke, 'snapshot'), \
                    patch.object(redroid_app_smoke.runtime_probe, 'manager_state',
                                 return_value={'scopeVerified': True}), \
                    patch.object(redroid_app_smoke.AppSmoke, 'capture', autospec=True,
                                 side_effect=lambda _self, enabled, profile:
                                 capture_events(enabled, profile)):
                report = instance.run_candidate(json.loads(candidate_path.read_text()))
        finally:
            redroid_app_smoke.smoke.configure_app_id(prior_app_id)

        return report, candidate, module

    @staticmethod
    def validate_historical_raw_evidence(report, evidence_root, candidate, module):
        observer_sha = report['requestIdentityEvidence']['disabled']['observerSha256']
        with patch.object(packet_capture, 'observer_source_digest', return_value=observer_sha):
            return runtime_task._validate_qq_raw_evidence(report, evidence_root, candidate, module)


if __name__ == '__main__':
    unittest.main()
