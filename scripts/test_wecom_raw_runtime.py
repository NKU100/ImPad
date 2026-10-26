import base64
import hashlib
import io
import json
import shutil
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts.ci import candidate_selection, redroid_app_smoke, redroid_ui_smoke, runtime_task
from scripts.ci.apps.wecom import runtime_policy
from scripts.testdata.runtime_replay import prepare_candidate_bundle


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / 'scripts/testdata/runtime-replay/wecom'
PACKAGE = 'com.tencent.wework'


def _bytes(path):
    return Path(path).read_bytes()


def _completed(args, code=0, stdout=b'', stderr=b''):
    import subprocess
    return subprocess.CompletedProcess(args, code, stdout, stderr)


def _activity(name):
    return (f'topResumedActivity=ActivityRecord{{fixture u0 {PACKAGE}/'
            f'com.tencent.wework.{name} t1}}\n')


def _wecom_logcat(candidate, module):
    plan = candidate['expectedPlan']
    dexkit = module['dexkit']
    native_path = '/data/app/io.github.nku100.impad/base.apk'
    rows = [
        '07-01 12:00:00.100  2727  2727 I ImPad: (com.tencent.wework)[io.github.nku100.impad,ImPad,test,1,1] '
        f"status=COMPATIBLE rule plan={plan['ruleId']}/{plan['strategyId']} "
        f"versionCode={candidate['identity']['versionCode']} apkInputs=1 cacheHit=false",
        '07-01 12:00:00.200  2727  2727 I ImPad: (com.tencent.wework)[io.github.nku100.impad,ImPad,test,1,1] '
        'installed 3 WeCom hooks',
        '07-01 12:00:00.300  2727  2727 I ImPad: (com.tencent.wework)[io.github.nku100.impad,ImPad,test,1,1] '
        'rule-runtime event=tinker_ready elapsedRealtimeNs=100 contextPackage=com.tencent.wework '
        'loaderClass=io.github.nku100.impad.TestClassLoader',
        '07-01 12:00:00.400  2727  2727 I ImPad: (com.tencent.wework)[io.github.nku100.impad,ImPad,test,1,1] '
        'rule-runtime event=native_loaded elapsedRealtimeNs=200 '
        f"apiSha256={dexkit['dexkitAndroidApiSha256']} "
        f"nativeSha256={dexkit['dexkitAndroidArm64NativeSha256']} nativeAbi=arm64-v8a "
        f'packagingMode=APK_DIRECT sourceApkPath={native_path} '
        f'nativeLoadPath={native_path}!/lib/arm64-v8a/libdexkit.so',
        '07-01 12:00:00.500  2727  2727 I ImPad: (com.tencent.wework)[io.github.nku100.impad,ImPad,test,1,1] '
        f"rule-runtime event=hook_installed elapsedRealtimeNs=300 ruleId={plan['ruleId']} "
        f"strategyId={plan['strategyId']} hookCount=3 processName={PACKAGE}",
    ]
    return '\n'.join(rows) + '\n'


class WeComPortableRuntimeReplayTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._fixture_tmp = tempfile.TemporaryDirectory(prefix='wecom-runtime-fixture-')
        _, cls.candidate_path, cls.candidate, _, _ = prepare_candidate_bundle(
            'wecom', Path(cls._fixture_tmp.name))
        cls.module = cls.candidate['module']

    @classmethod
    def tearDownClass(cls):
        cls._fixture_tmp.cleanup()

    def test_pinned_production_raw_replays_ui_qr_and_main_process_hook_claims(self):
        samples = {}
        for label, filename in {
            'baseline': 'module-off-login',
            'enabled': 'module-on-login',
            'qrCurrent': 'tablet-qr-current',
            'qrConfirmed': 'tablet-qr-confirmed',
        }.items():
            samples[label] = {
                'activity': _activity('LoginWxAuthActivity' if label in ('baseline', 'enabled')
                                      else 'LoginQrCodeForAndroidPad'),
                'hierarchy': (FIXTURE / f'{filename}.xml').read_text(),
            }
        ui = runtime_policy.derive_ui_evidence(
            samples, PACKAGE, 'LoginQrCodeForAndroidPad')
        self.assertTrue(ui['baseline']['loginPageReady'])
        self.assertFalse(ui['baseline']['tabletEntry'])
        self.assertTrue(ui['enabled']['loginPageReady'])
        self.assertTrue(ui['enabled']['tabletEntry'])
        self.assertEqual('LoginQrCodeForAndroidPad', ui['qrPage'])
        self.assertTrue(ui['qrStable'])

        _, lifecycle = runtime_policy.derive_hook_evidence(
            (FIXTURE / 'processes.txt').read_text(),
            _wecom_logcat(self.candidate, self.module),
            self.candidate, self.module)
        self.assertEqual('READY', lifecycle['tinkerEvidence']['status'])
        self.assertEqual('LOADED', lifecycle['nativeEvidence']['status'])
        self.assertEqual(3, lifecycle['hookCount'])
        placeholder = Path(self._fixture_tmp.name) / 'placeholder.json'
        placeholder.write_text('{}\n')
        placeholder_hash = hashlib.sha256(placeholder.read_bytes()).hexdigest()
        with self.assertRaisesRegex(ValueError, 'missing required'):
            runtime_task._validate_wecom_raw_evidence(
                {'evidence': [{
                    'path': 'placeholder.json', 'sha256': placeholder_hash,
                }]},
                Path(self._fixture_tmp.name), self.candidate, self.module)

    def _write_candidate_bundle(self, root):
        candidate_root, candidate_path, _, _, module_apk = prepare_candidate_bundle('wecom', root)

        acceptance = root / 'compatibility/wecom/acceptance.json'
        acceptance.parent.mkdir(parents=True)
        acceptance.write_text(json.dumps({'schemaVersion': 1, 'appId': 'wecom', 'records': []}) + '\n')
        regression = [{
            'appId': 'wecom',
            'status': 'NO_HISTORICAL_SAMPLES',
            'acceptanceSnapshotSha256': hashlib.sha256(acceptance.read_bytes()).hexdigest(),
            'initialMigration': True,
        }]
        regression_path = candidate_root / 'regression.json'
        regression_path.write_text(json.dumps(regression) + '\n')
        return (candidate_root, candidate_path, module_apk,
                regression_path, acceptance)

    def _produce_from_fixture_ui_and_log_capture(self, root, missing_entry=False):
        candidate_root, candidate_path, module_apk, regression_path, acceptance_path = self._write_candidate_bundle(root)
        output = root / 'runtime'
        evidence_dir = output / 'apps/wecom'
        evidence_dir.mkdir(parents=True)
        retained = (
            'module-off-login.xml', 'module-off-login-activity.txt',
            'module-on-login.xml', 'module-on-login-activity.txt',
            'tablet-qr-current.xml', 'tablet-qr-current-activity.txt',
            'tablet-qr-confirmed.xml', 'tablet-qr-confirmed-activity.txt',
            'logcat.txt', 'processes.txt', 'manager-module-state.json',
        )
        for name in retained:
            if name.endswith('-activity.txt'):
                label = name.removesuffix('-activity.txt')
                activity = 'LoginWxAuthActivity' if label in ('module-off-login', 'module-on-login') \
                    else 'LoginQrCodeForAndroidPad'
                (evidence_dir / name).write_text(_activity(activity))
            elif name == 'logcat.txt':
                fixture_candidate = json.loads((candidate_root / 'candidate-report.json').read_text())
                (evidence_dir / name).write_text(_wecom_logcat(fixture_candidate, fixture_candidate['module']))
            elif name == 'processes.txt':
                shutil.copyfile(FIXTURE / name, evidence_dir / name)
            elif name.endswith('.xml'):
                shutil.copyfile(FIXTURE / name, evidence_dir / name)
            else:
                (evidence_dir / name).write_text('{}\n')

        if missing_entry:
            (evidence_dir / 'module-on-login.xml').write_bytes(
                (evidence_dir / 'module-off-login.xml').read_bytes())

        source_report = json.loads((FIXTURE / 'target.json').read_text())
        full_logcat = (evidence_dir / 'logcat.txt').read_bytes()
        processes = (FIXTURE / 'processes.txt').read_bytes()
        state_values = [True, False, True, True]
        state_index = 0
        temp_db = root / 'manager-db'
        temp_db.mkdir()
        database_bytes = []
        for enabled in state_values:
            database = temp_db / f'{int(enabled)}-{len(database_bytes)}.sqlite'
            connection = sqlite3.connect(database)
            try:
                connection.execute('CREATE TABLE modules_state(module_pkg_name TEXT, user_id INTEGER, enabled INTEGER)')
                connection.execute('INSERT INTO modules_state VALUES(?,?,?)',
                                   ('io.github.nku100.impad', 0, int(enabled)))
                connection.execute('CREATE TABLE scope(module_pkg_name TEXT, app_pkg_name TEXT, user_id INTEGER)')
                connection.execute('INSERT INTO scope VALUES(?,?,?)',
                                   ('io.github.nku100.impad', PACKAGE, 0))
                connection.commit()
            finally:
                connection.close()
            database_bytes.append(database.read_bytes())

        def manager_su(command, **kwargs):
            nonlocal state_index
            if command == 'ps -A -o PID,NAME':
                return _completed(command, stdout=processes)
            if command.startswith('base64 /data/adb/lspd/config/modules_config.db'):
                if command.endswith('-wal'):
                    return _completed(command, 1, b'', b'no WAL file')
                payload = base64.encodebytes(database_bytes[state_index]).replace(b'\n', b'')
                state_index += 1
                return _completed(command, stdout=payload)
            raise AssertionError(f'Unexpected manager command: {command}')

        def adb(*args, **kwargs):
            if args[:3] == ('logcat', '-d', '-v'):
                return _completed(args, stdout=full_logcat)
            return _completed(args)

        def mobile_input(label):
            return (evidence_dir / f'{label}.xml').read_text()

        def snapshot(label):
            return ((evidence_dir / f'{label}-activity.txt').read_text(),
                    (evidence_dir / f'{label}.xml').read_text())

        candidate = json.loads((candidate_root / 'candidate-report.json').read_text())
        module = candidate['module']
        identity = candidate['identity']
        native_line = next(line for line in _wecom_logcat(candidate, module).splitlines()
                           if 'event=native_loaded' in line)
        module_path = native_line.split('sourceApkPath=', 1)[1].split()[0]
        inputs = {'wecom': {'identity': identity, 'apkFiles': candidate['apkFiles'], 'signerVerified': True,
                            'candidateReport': candidate, 'moduleIdentity': module}}
        installed = {
            'app': {'packageName': PACKAGE, 'baseApkPath': '/data/app/wecom/base.apk',
                    'baseApkSha256': identity['apkSha256']},
            'module': {'packageName': 'io.github.nku100.impad', 'baseApkPath': module_path,
                       'baseApkSha256': module['sha256']},
        }
        with patch.object(redroid_ui_smoke, 'query_target', return_value=source_report['target']), \
                patch.object(redroid_ui_smoke.s, 'adb', side_effect=adb), \
                patch.object(redroid_ui_smoke.s, 'su', side_effect=manager_su), \
                patch.object(redroid_ui_smoke.s, 'configure_manager'), \
                patch.object(redroid_ui_smoke.s, 'mobile_input', side_effect=mobile_input), \
                patch.object(redroid_ui_smoke.s, 'snapshot', side_effect=snapshot), \
                patch.object(redroid_ui_smoke.s, 'tap'), \
                patch.object(redroid_ui_smoke.runtime_probe, 'installed_identity',
                             side_effect=lambda _smoke, package: installed['module']
                             if package == 'io.github.nku100.impad' else installed['app']), \
                patch.object(redroid_ui_smoke.time, 'sleep'), \
                redirect_stdout(io.StringIO()):
            report = redroid_ui_smoke.probe('wecom', output / 'apps', inputs, module['sha256'])

        self.assertEqual('RUNTIME_REJECTED' if missing_entry else 'RUNTIME_SMOKE_VERIFIED', report['status'])
        self.assertEqual(4, state_index)
        pipeline = redroid_app_smoke.pipeline_report(
            report, candidate, module, output)
        report_dir = output / 'evidence'
        report_dir.mkdir()
        smoke_path = report_dir / 'smoke-report.json'
        smoke_path.write_text(json.dumps(pipeline, indent=2) + '\n')
        return candidate_path, module_apk, regression_path, smoke_path, output, acceptance_path, pipeline

    def test_failed_production_probe_reaches_file_verified_runtime_incompatibility(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            candidate_path, module_apk, regression_path, smoke_path, output, acceptance_path, pipeline = \
                self._produce_from_fixture_ui_and_log_capture(root, missing_entry=True)
            self.assertTrue(pipeline['evidence'])
            report = runtime_task.verify_validation_files(
                candidate_path, module_apk, regression_path, smoke_path, output,
                'failure', ROOT, acceptance_path, initial_migration=True)
            self.assertEqual('RUNTIME_INCOMPATIBLE', report['outcome'])
            self.assertEqual('runtime', report['failedStage'])
            self.assertNotIn('rejectionReplayError', report['runtime'])
            nested = candidate_path.parent / 'evidence/runtime-smoke'
            shutil.copytree(output, nested)
            replayed = runtime_task.verify_validation_files(
                candidate_path, module_apk, regression_path, nested / 'evidence/smoke-report.json', nested,
                'failure', ROOT, acceptance_path, initial_migration=True)
            replayed.pop('_acceptanceSnapshotObject', None)
            report_path = candidate_path.parent / 'runtime-candidate-report.json'
            report_path.write_text(json.dumps(replayed))
            self.assertTrue(runtime_task.export_runtime_adaptation(
                report_path, candidate_path, nested, 'adapt'))
            exported = json.loads(candidate_path.read_text())
            self.assertEqual('INCOMPATIBLE', exported['status'])
            from scripts.ci.codex_adaptation import copy_runtime_failure
            analysis = root / 'agent-analysis'
            copy_runtime_failure(exported, candidate_path.parent, analysis)
            self.assertTrue((analysis / 'evidence/runtime-smoke/apps/wecom/module-on-login.xml').is_file())
            # Restore the original prepared candidate for the identity-mismatch check.
            candidate_path.write_text(json.dumps(report['candidate']))
            raw = json.loads(smoke_path.read_text())
            raw['moduleApkSha256'] = 'f' * 64
            smoke_path.write_text(json.dumps(raw))
            rejected = runtime_task.verify_validation_files(
                candidate_path, module_apk, regression_path, smoke_path, output,
                'failure', ROOT, acceptance_path, initial_migration=True)
            self.assertEqual('ERROR', rejected['outcome'])

    def test_production_probe_raw_report_verifier_and_evaluator_share_the_saved_ui_capture(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            candidate_path, module_apk, regression_path, smoke_path, output, acceptance_path, pipeline = \
                self._produce_from_fixture_ui_and_log_capture(root)
            verified = runtime_task.verify_validation_files(
                candidate_path, module_apk, regression_path, smoke_path, output,
                'success', ROOT, acceptance_path, initial_migration=True)
            self.assertEqual('VERIFIED', verified['outcome'])
            lifecycle = verified['runtime']['runtimeLifecycleEvidence']
            self.assertEqual('READY', lifecycle['tinkerEvidence']['status'])
            self.assertEqual('LOADED', lifecycle['nativeEvidence']['status'])
            self.assertEqual(3, lifecycle['hookEvidence']['hookCount'])

            failed_outer_gate = runtime_task.evaluate_validation(
                verified['candidate'], verified['module'], verified['static'],
                verified['regression'], verified['runtime'], 'failure')
            self.assertEqual('ERROR', failed_outer_gate['outcome'])
            self.assertEqual('runtime', failed_outer_gate['failedStage'])

    def test_wecom_raw_verifier_rejects_summary_ui_activity_and_scope_tampering(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, _, _, _, output, _, pipeline = self._produce_from_fixture_ui_and_log_capture(root)
            candidate = self.candidate
            module = self.module

            cases = {
                'summary entry claim': ('summary', 'baselineEvidence'),
                'module-off hierarchy replacement': ('apps/wecom/module-off-login.xml', None),
                'module-on hierarchy replacement': ('apps/wecom/module-on-login.xml', None),
                'wrong QR hierarchy package': ('apps/wecom/tablet-qr-current.xml', None),
                'wrong QR activity': ('apps/wecom/tablet-qr-confirmed-activity.txt', None),
                'hook log package scope': ('apps/wecom/app-hook-logs.txt', None),
                'hook log owner PID': ('apps/wecom/app-hook-logs.txt', None),
                'disabled manager state': ('apps/wecom/manager-state-disabled.json', None),
                'installed module identity': ('apps/wecom/installed-identities.json', None),
                'native library hash': ('apps/wecom/app-hook-logs.txt', None),
            }
            for label, (relative, _) in cases.items():
                with self.subTest(case=label):
                    case_root = root / label.replace(' ', '-')
                    shutil.copytree(output, case_root)
                    smoke_path = case_root / 'evidence/smoke-report.json'
                    smoke = json.loads(smoke_path.read_text())
                    if relative == 'summary':
                        smoke['baselineEvidence']['tabletEntry'] = True
                        smoke_path.write_text(json.dumps(smoke, indent=2) + '\n')
                    elif label in ('module-off hierarchy replacement',
                                   'module-on hierarchy replacement'):
                        source_name = ('module-on-login.xml'
                                       if label == 'module-off hierarchy replacement'
                                       else 'module-off-login.xml')
                        source = case_root / f'apps/wecom/{source_name}'
                        target = case_root / relative
                        target.write_bytes(source.read_bytes())
                        self._refresh_reference(smoke, relative, target)
                    elif label == 'wrong QR hierarchy package':
                        target = case_root / relative
                        target.write_text(target.read_text().replace(PACKAGE, 'org.example.foreign'))
                        self._refresh_reference(smoke, relative, target)
                    elif label == 'wrong QR activity':
                        target = case_root / relative
                        target.write_text(target.read_text().replace('LoginQrCodeForAndroidPad', 'ForeignActivity'))
                        self._refresh_reference(smoke, relative, target)
                    elif label == 'hook log package scope':
                        target = case_root / 'apps/wecom/logcat.txt'
                        target.write_text(target.read_text().replace(f'({PACKAGE})', '(org.example.foreign)'))
                        scoped = runtime_policy.scoped_module_log_lines(
                            target.read_text(), (case_root / 'apps/wecom/processes.txt').read_text(), PACKAGE)
                        (case_root / 'apps/wecom/app-hook-logs.txt').write_text(scoped)
                        self._refresh_reference(smoke, 'apps/wecom/logcat.txt', target)
                        self._refresh_reference(smoke, 'apps/wecom/app-hook-logs.txt',
                                                case_root / 'apps/wecom/app-hook-logs.txt')
                    elif label == 'hook log owner PID':
                        target = case_root / 'apps/wecom/logcat.txt'
                        target.write_text(target.read_text().replace(' 2727  2727 ', ' 9999  9999 '))
                        scoped = runtime_policy.scoped_module_log_lines(
                            target.read_text(), (case_root / 'apps/wecom/processes.txt').read_text(), PACKAGE)
                        scoped_path = case_root / 'apps/wecom/app-hook-logs.txt'
                        scoped_path.write_text(scoped)
                        self._refresh_reference(smoke, 'apps/wecom/logcat.txt', target)
                        self._refresh_reference(smoke, 'apps/wecom/app-hook-logs.txt', scoped_path)
                    elif label == 'disabled manager state':
                        target = case_root / relative
                        state = json.loads(target.read_text())
                        state['moduleStateRows'][0]['enabled'] = 1
                        target.write_text(json.dumps(state) + '\n')
                        self._refresh_reference(smoke, relative, target)
                    elif label == 'installed module identity':
                        target = case_root / relative
                        identity = json.loads(target.read_text())
                        identity['module']['baseApkSha256'] = '0' * 64
                        target.write_text(json.dumps(identity) + '\n')
                        self._refresh_reference(smoke, relative, target)
                    else:
                        target = case_root / 'apps/wecom/logcat.txt'
                        target.write_text(target.read_text().replace(
                            self.module['dexkit']['dexkitAndroidArm64NativeSha256'], '0' * 64))
                        scoped = runtime_policy.scoped_module_log_lines(
                            target.read_text(), (case_root / 'apps/wecom/processes.txt').read_text(), PACKAGE)
                        scoped_path = case_root / 'apps/wecom/app-hook-logs.txt'
                        scoped_path.write_text(scoped)
                        self._refresh_reference(smoke, 'apps/wecom/logcat.txt', target)
                        self._refresh_reference(smoke, 'apps/wecom/app-hook-logs.txt', scoped_path)
                    smoke_path.write_text(json.dumps(smoke, indent=2) + '\n')
                    with self.assertRaises(ValueError):
                        runtime_task._validate_wecom_raw_evidence(smoke, case_root, candidate, module)

    def test_raw_verifier_rejects_missing_second_qr_sample(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, _, _, _, output, _, _ = self._produce_from_fixture_ui_and_log_capture(root)
            candidate = self.candidate
            module = self.module
            smoke = json.loads((output / 'evidence/smoke-report.json').read_text())
            smoke['evidence'] = [row for row in smoke['evidence']
                                 if row['path'] not in {
                                     'apps/wecom/tablet-qr-confirmed.xml',
                                     'apps/wecom/tablet-qr-confirmed-activity.txt'}]
            with self.assertRaises(ValueError):
                runtime_task._validate_wecom_raw_evidence(smoke, output, candidate, module)

    def test_file_verifier_rejects_changed_module_apk_and_source_tree(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            candidate_path, module_apk, regression_path, smoke_path, output, acceptance_path, _ = \
                self._produce_from_fixture_ui_and_log_capture(root)

            original_apk = module_apk.read_bytes()
            module_apk.write_bytes(original_apk + b'tampered')
            with self.assertRaisesRegex(ValueError, 'Candidate module identity differs'):
                runtime_task.verify_validation_files(
                    candidate_path, module_apk, regression_path, smoke_path, output,
                    'success', ROOT, acceptance_path, initial_migration=True)
            module_apk.write_bytes(original_apk)

            changed_repository = root / 'changed-source-tree'
            for row in candidate_selection.module_source_hashes(ROOT):
                source = ROOT / row['path']
                target = changed_repository / row['path']
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)
            changed_source = changed_repository / 'scripts/ci/runtime_task.py'
            changed_source.write_bytes(changed_source.read_bytes() + b'\n# changed test input\n')
            with self.assertRaisesRegex(ValueError, 'fingerprint does not match current source'):
                runtime_task.verify_validation_files(
                    candidate_path, module_apk, regression_path, smoke_path, output,
                    'success', changed_repository, acceptance_path, initial_migration=True)

    @staticmethod
    def _refresh_reference(smoke, relative, path):
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        for row in smoke['evidence']:
            if row['path'] == relative:
                row['sha256'] = digest
                return
        raise AssertionError(f'Missing evidence reference {relative}')


if __name__ == '__main__':
    unittest.main()
