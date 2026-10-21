import base64
import hashlib
import io
import json
import os
import shutil
import sqlite3
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts.ci import candidate_selection, redroid_app_smoke, redroid_ui_smoke, runtime_task
from scripts.ci.apps.wecom import runtime_policy


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / 'evidence/task-6-wecom-runtime/round-1/runtime/attempt-2/probe'
WECom_RAW = RUNTIME / 'apps/wecom'
BUNDLE = ROOT / 'evidence/task-6-wecom-runtime/round-1/bundle'
PACKAGE = 'com.tencent.wework'


def _bytes(path):
    return Path(path).read_bytes()


def _completed(args, code=0, stdout=b'', stderr=b''):
    import subprocess
    return subprocess.CompletedProcess(args, code, stdout, stderr)


class WeComSavedRawReplayTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.original_summary = json.loads((RUNTIME / 'evidence/smoke-report.json').read_text())
        cls.candidate = json.loads((BUNDLE / 'candidate-report.json').read_text())
        cls.module = json.loads((BUNDLE / 'module-identity.json').read_text())

    def test_saved_production_raw_replays_ui_qr_and_main_process_hook_claims(self):
        samples = {}
        for label, filename in {
            'baseline': 'module-off-login',
            'enabled': 'module-on-login',
            'qrCurrent': 'tablet-qr-current',
            'qrConfirmed': 'tablet-qr-confirmed',
        }.items():
            samples[label] = {
                'activity': (WECom_RAW / f'{filename}-activity.txt').read_text(),
                'hierarchy': (WECom_RAW / f'{filename}.xml').read_text(),
            }
        ui = runtime_policy.derive_ui_evidence(
            samples, PACKAGE, 'LoginQrCodeForAndroidPad')
        self.assertEqual(self.original_summary['baselineEvidence']['tabletEntry'],
                         ui['baseline']['tabletEntry'])
        self.assertEqual(self.original_summary['enabled']['tabletEntry'],
                         ui['enabled']['tabletEntry'])
        self.assertEqual('LoginQrCodeForAndroidPad', ui['qrPage'])
        self.assertTrue(ui['qrStable'])

        diagnostics, lifecycle = runtime_policy.derive_hook_evidence(
            (WECom_RAW / 'processes.txt').read_text(),
            (WECom_RAW / 'app-hook-logs.txt').read_text(),
            self.candidate, self.module)
        self.assertEqual(self.original_summary['runtimeDiagnostics'], diagnostics)
        self.assertEqual('READY', lifecycle['tinkerEvidence']['status'])
        self.assertEqual('LOADED', lifecycle['nativeEvidence']['status'])
        self.assertEqual(3, lifecycle['hookCount'])
        with self.assertRaisesRegex(ValueError, 'missing required'):
            runtime_task._validate_wecom_raw_evidence(
                self.original_summary, RUNTIME, self.candidate, self.module)

    def _write_candidate_bundle(self, root):
        candidate_root = root / 'candidate'
        candidate_root.mkdir()
        (candidate_root / 'module').mkdir()
        (candidate_root / 'apk-inputs').mkdir()
        for rel in ('candidate-report.json', 'static-check.json'):
            shutil.copyfile(BUNDLE / rel, candidate_root / rel)
        candidate = json.loads((candidate_root / 'candidate-report.json').read_text())
        apk_rel = candidate['apkFiles'][0]['path']
        (candidate_root / apk_rel).parent.mkdir(parents=True, exist_ok=True)
        os.link(BUNDLE / apk_rel, candidate_root / apk_rel)
        module_apk = candidate_root / 'module/app-debug.apk'

        fingerprint = self._current_module_fingerprint(ROOT)
        fingerprint_bytes = (json.dumps(fingerprint, indent=2) + '\n').encode()
        with zipfile.ZipFile(module_apk, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('assets/runtime-identity.json', fingerprint_bytes)
            archive.writestr('classes.dex', b'dex\n035\0')

        static_path = candidate_root / candidate['staticReport']
        static = json.loads(static_path.read_text())
        execution = static['executionIdentity']
        execution.update(
            fingerprintFileSha256=hashlib.sha256(fingerprint_bytes).hexdigest(),
            sourceTreeSha256=fingerprint['sourceTreeSha256'],
            routeConfigurationSha256=fingerprint['routeConfigurationSha256'],
            dependencyLockSha256=fingerprint['dependencyLockSha256'],
        )
        static_path.write_text(json.dumps(static, indent=2) + '\n')
        candidate['staticReportSha256'] = hashlib.sha256(static_path.read_bytes()).hexdigest()
        module_identity = candidate_selection.prepare_module(module_apk, static, ROOT)
        candidate['module'] = module_identity
        (candidate_root / 'candidate-report.json').write_text(json.dumps(candidate, indent=2) + '\n')

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
        return (candidate_root, candidate_root / 'candidate-report.json', module_apk,
                regression_path, acceptance)

    @staticmethod
    def _source_rows(root, paths):
        return [
            {'path': path.relative_to(root).as_posix(),
             'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
            for path in sorted(set(paths), key=lambda item: item.relative_to(root).as_posix())
        ]

    @classmethod
    def _current_module_fingerprint(cls, root):
        root = Path(root).resolve()
        module_sources = candidate_selection.module_source_hashes(root)

        def kotlin_sources(directory):
            return list((root / directory).rglob('*.kt'))

        resolver_sources = cls._source_rows(
            root, kotlin_sources('compat-core/src/main/kotlin/io/github/nku100/impad/compat/rules'))
        android_resolver_paths = [root / path for path in (
            'app/src/main/kotlin/io/github/nku100/impad/runtime/TargetMethodResolver.kt',
            'app/src/main/kotlin/io/github/nku100/impad/runtime/RuleRuntimeResolver.kt',
            'app/src/main/kotlin/io/github/nku100/impad/runtime/RuleResolutionCache.kt',
            'app/src/main/kotlin/io/github/nku100/impad/runtime/InstalledBuildIdentityReader.kt',
            'app/src/main/kotlin/io/github/nku100/impad/runtime/AndroidDexKitRuntime.kt',
            'app/src/main/kotlin/io/github/nku100/impad/runtime/RuleInstallCoordinator.kt',
            'app/src/main/kotlin/io/github/nku100/impad/ImPadModule.kt',
        )]
        resolver_sources.extend(cls._source_rows(root, android_resolver_paths))
        strategy_paths = (kotlin_sources('app/src/main/kotlin/io/github/nku100/impad/apps/qq')
                          + kotlin_sources('app/src/main/kotlin/io/github/nku100/impad/apps/wecom')
                          + [root / path for path in (
                              'app/src/main/kotlin/io/github/nku100/impad/runtime/HookInstaller.kt',
                              'app/src/main/kotlin/io/github/nku100/impad/runtime/HookCall.kt',
                          )])
        lock_bytes = (root / 'gradle/dexkit-lock.json').read_bytes()
        lock = json.loads(lock_bytes)
        artifacts = lock['artifacts']
        return {
            'schemaVersion': 1,
            'ruleSources': cls._source_rows(root, kotlin_sources('compat-rules/src/main/kotlin')),
            'resolverSources': resolver_sources,
            'strategySources': cls._source_rows(root, strategy_paths),
            'moduleSources': module_sources,
            'sourceTreeSha256': candidate_selection.source_tree_sha256(root),
            'routeConfigurationSha256': candidate_selection.route_configuration_sha256(module_sources),
            'dependencyLockSha256': candidate_selection.dependency_lock_sha256(module_sources),
            'dexkitLockSha256': hashlib.sha256(lock_bytes).hexdigest(),
            'dexkitSourceCommit': lock['source']['commit'],
            'dexkitApiSha256': artifacts['jvmApi']['sha256'],
            'dexkitAndroidApiSha256': artifacts['androidAar']['sha256'],
            'dexkitLinuxAarch64NativeSha256': artifacts['linuxAarch64']['sha256'],
            'dexkitLinuxX8664NativeSha256': artifacts['linuxX8664']['sha256'],
            'dexkitAndroidArm64NativeSha256': artifacts['androidArm64V8a']['sha256'],
        }

    def _produce_from_saved_ui_and_log_capture(self, root):
        candidate_root, candidate_path, module_apk, regression_path, acceptance_path = self._write_candidate_bundle(root)
        output = root / 'runtime'
        evidence_dir = output / 'apps/wecom'
        evidence_dir.mkdir(parents=True)
        retained = (
            'module-off-login.xml', 'module-off-login-activity.txt',
            'module-on-login.xml', 'module-on-login-activity.txt',
            'tablet-qr-current.xml', 'tablet-qr-current-activity.txt', 'tablet-qr-current.png',
            'tablet-qr-confirmed.xml', 'tablet-qr-confirmed-activity.txt', 'tablet-qr-confirmed.png',
            'logcat.txt', 'processes.txt', 'manager-module-state.json',
        )
        for name in retained:
            shutil.copyfile(WECom_RAW / name, evidence_dir / name)

        source_report = json.loads((WECom_RAW / 'report.json').read_text())
        full_logcat = (WECom_RAW / 'logcat.txt').read_bytes()
        processes = (WECom_RAW / 'processes.txt').read_bytes()
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
        native_line = next(line for line in (WECom_RAW / 'app-hook-logs.txt').read_text().splitlines()
                           if 'event=native_loaded' in line)
        module_path = native_line.split('sourceApkPath=', 1)[1].split()[0]
        inputs = {'wecom': {'identity': identity, 'signerVerified': True,
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

        self.assertEqual('RUNTIME_SMOKE_VERIFIED', report['status'])
        self.assertEqual(4, state_index)
        pipeline = redroid_app_smoke.pipeline_report(
            report, candidate, module, output)
        report_dir = output / 'evidence'
        report_dir.mkdir()
        smoke_path = report_dir / 'smoke-report.json'
        smoke_path.write_text(json.dumps(pipeline, indent=2) + '\n')
        return candidate_path, module_apk, regression_path, smoke_path, output, acceptance_path, pipeline

    def test_production_probe_raw_report_verifier_and_evaluator_share_the_saved_ui_capture(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            candidate_path, module_apk, regression_path, smoke_path, output, acceptance_path, pipeline = \
                self._produce_from_saved_ui_and_log_capture(root)
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
            _, _, _, _, output, _, pipeline = self._produce_from_saved_ui_and_log_capture(root)
            candidate = json.loads((BUNDLE / 'candidate-report.json').read_text())
            module = json.loads((BUNDLE / 'module-identity.json').read_text())

            cases = {
                'summary entry claim': ('summary', 'baselineEvidence'),
                'tablet-on hierarchy replacement': ('apps/wecom/module-on-login.xml', None),
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
                    elif label == 'tablet-on hierarchy replacement':
                        source = case_root / 'apps/wecom/module-off-login.xml'
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
            _, _, _, _, output, _, _ = self._produce_from_saved_ui_and_log_capture(root)
            candidate = json.loads((BUNDLE / 'candidate-report.json').read_text())
            module = json.loads((BUNDLE / 'module-identity.json').read_text())
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
                self._produce_from_saved_ui_and_log_capture(root)

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
