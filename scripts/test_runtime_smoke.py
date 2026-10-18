import importlib.util
import os
from pathlib import Path
import unittest
from unittest.mock import patch
import subprocess
import tempfile
import sys
import zipfile
import xml.etree.ElementTree as ET
from contextlib import ExitStack, redirect_stdout
import io
from scripts.ci.apps.qq import runtime_policy as qq_ui

spec = importlib.util.spec_from_file_location('smoke', Path(__file__).parent / 'ci/runtime_smoke.py')
smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smoke)


class UiEvidenceTest(unittest.TestCase):
    def test_wechat_launch_resolves_activity_without_monkey(self):
        from scripts.ci.apps.wechat import runtime_policy as wechat_ui
        xml = '<hierarchy><node package="com.tencent.mm"/></hierarchy>'
        with patch.object(smoke, 'APP_PACKAGE', 'com.tencent.mm'), \
             patch.object(smoke, 'UI_STRATEGY', wechat_ui), \
             patch.object(smoke, 'enable_page_size_backcompat'), \
             patch.object(smoke, 'snapshot', return_value=('MobileInputUI', xml)), \
             patch.object(smoke.time, 'sleep'), patch.object(smoke, 'adb',
                 return_value=subprocess.CompletedProcess([], 0, b'com.tencent.mm/.ui.LauncherUI\n', b'')) as adb:
            with patch.dict(wechat_ui.__dict__):
                wechat_ui.__dict__.pop('launcher_component', None)
                smoke.mobile_input('baseline-mobile')
            self.assertIn(('shell', 'am', 'start', '-n', 'com.tencent.mm/.ui.LauncherUI'),
                          [call.args for call in adb.call_args_list])
            self.assertFalse(any('monkey' in call.args for call in adb.call_args_list))

    def test_ocr_label_ignores_adjacent_icon_noise_but_rejects_duplicate_labels(self):
        header = 'level\tpage_num\tblock_num\tpar_num\tline_num\tleft\ttop\twidth\theight\tconf\ttext\n'
        rows = '5\t1\t1\t1\t1\t158\t670\t32\t15\t36\t2s\n5\t1\t1\t1\t1\t230\t654\t55\t35\t82\tI\'m\n5\t1\t1\t1\t1\t302\t654\t72\t35\t94\tPad\n'
        self.assertEqual('[230,654][374,689]', smoke.ocr_target(header + rows, "I'm Pad").get('bounds'))
        with self.assertRaisesRegex(ValueError, 'ambiguous'):
            smoke.ocr_target(header + rows + rows.replace('\t1\t1\t1\t1\t', '\t1\t2\t1\t1\t'), "I'm Pad")

    def test_qq_navigation_uses_accessibility_and_rejects_failed_actions(self):
        button = ET.fromstring('<node package="com.tencent.mobileqq" text="同意" clickable="true" enabled="true" bounds="[540,1418][939,1564]"/>')
        for code in (0, 1):
            with self.subTest(code=code), patch.object(smoke, 'APP_ID', 'qq'), \
                 patch.object(smoke, 'APP_PACKAGE', 'com.tencent.mobileqq'), \
                 patch.object(smoke, 'UI_STRATEGY', qq_ui), patch.object(smoke, 'save'), \
                 patch.object(smoke.time, 'sleep'), patch.object(smoke, 'adb',
                    return_value=subprocess.CompletedProcess([], code, b'', b'')) as adb:
                self.assertEqual(code == 0, smoke.tap(button, accessibility=True))
                self.assertEqual(('click', 'com.tencent.mobileqq', '同意'), adb.call_args.args[-3:])
                self.assertNotIn('input', adb.call_args.args)
                button.set('text', '发送验证码')
                with self.assertRaises(ValueError):
                    smoke.tap(button, accessibility=True)
                button.set('text', '同意')

    def test_qq_navigation_timeout_saves_input_window_display_and_power(self):
        xml = '<hierarchy><node package="com.tencent.mobileqq" text="同意"/></hierarchy>'
        with patch.object(smoke, 'APP_ID', 'qq'), patch.object(smoke, 'APP_PACKAGE', 'com.tencent.mobileqq'), \
             patch.object(smoke, 'UI_STRATEGY', qq_ui), patch.object(smoke, 'enable_page_size_backcompat'), \
             patch.object(smoke, 'snapshot', return_value=('SplashActivity', xml)), \
             patch.object(smoke, 'tap'), patch.object(smoke.time, 'sleep'), \
             patch.object(smoke, 'save') as save, patch.object(smoke, 'adb'):
            with self.assertRaisesRegex(RuntimeError, 'stable, rendered login input UI'):
                smoke.mobile_input('module-mobile')
            self.assertEqual(['module-mobile-' + service + '.txt' for service in ('input', 'window', 'display', 'power')],
                             [call.args[0] for call in save.call_args_list])

    def test_already_enabled_manager_is_not_toggled_off(self):
        with patch.object(smoke, 'adb'), patch.object(smoke, 'manager_module_enabled', return_value=True), patch.object(smoke, 'tap') as tap:
            smoke.configure_manager()
        tap.assert_not_called()

    def test_disabled_manager_is_enabled_once_and_state_confirmed(self):
        node=ET.Element('node', {'bounds':'[1,1][5,5]'})
        with patch.object(smoke, 'adb'), patch.object(smoke, 'manager_module_enabled', side_effect=[False,True]), patch.object(smoke, 'snapshot', return_value=('', '<hierarchy/>')), patch.object(smoke, 'manager_ocr', return_value='Enable module'), patch.object(smoke, 'ocr_target', return_value=node), patch.object(smoke, 'tap') as tap:
            smoke.configure_manager()
        tap.assert_called_once_with(node)

    def test_manager_state_survives_text_adb_transport(self):
        import base64,sqlite3
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'db'
            with sqlite3.connect(path) as connection:
                connection.execute('CREATE TABLE modules_state(module_pkg_name TEXT, user_id INTEGER, enabled INTEGER)')
                connection.execute('INSERT INTO modules_state VALUES(?,0,1)', (smoke.MODULE,))
            payload=base64.encodebytes(path.read_bytes()).replace(b'\n', b'\r\n')
            def read(command, **kwargs):
                return subprocess.CompletedProcess([], 1, b'', b'no wal') if command.endswith('-wal') else subprocess.CompletedProcess([],0,payload,b'')
            with patch.object(smoke,'EVIDENCE',Path(root)), patch.object(smoke,'su',side_effect=read):
                self.assertIs(True, smoke.manager_module_enabled())

    def test_manager_database_state_is_package_specific_and_read_only(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)/'modules.db'
            with sqlite3.connect(path) as connection:
                connection.execute('CREATE TABLE modules_state(module_pkg_name TEXT, user_id INTEGER, enabled INTEGER)')
                connection.executemany('INSERT INTO modules_state VALUES(?,?,?)', [(smoke.MODULE,0,0), (smoke.MODULE,10,1), ('other',0,1)])
            before=path.read_bytes()
            self.assertIs(False, smoke.module_enabled_from_database(path))
            self.assertEqual(before, path.read_bytes())

    def test_manager_state_reads_schema_and_enable_flag_from_wal(self):
        import base64, sqlite3
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'source.db'
            connection = sqlite3.connect(path)
            try:
                connection.execute('PRAGMA journal_mode=WAL')
                connection.execute('CREATE TABLE modules_state(module_pkg_name TEXT, user_id INTEGER, enabled INTEGER)')
                connection.execute('INSERT INTO modules_state VALUES(?,0,1)', (smoke.MODULE,))
                connection.commit()
                payloads = {suffix: base64.encodebytes(Path(str(path) + suffix).read_bytes()) for suffix in ('', '-wal')}
                def read(command, **kwargs):
                    suffix = '-wal' if command.endswith('-wal') else ''
                    return subprocess.CompletedProcess([], 0, payloads[suffix], b'')
                with patch.object(smoke, 'EVIDENCE', Path(root)), patch.object(smoke, 'su', side_effect=read):
                    self.assertIs(True, smoke.manager_module_enabled())
            finally:
                connection.close()

    def test_module_package_scope_entry_and_display_metadata(self):
        root = Path(__file__).resolve().parents[1]
        resources = ET.parse(root / 'app/src/main/res/values/strings.xml').getroot()
        strings = {entry.get('name'): ''.join(entry.itertext()) for entry in resources.findall('string')}
        self.assertEqual(smoke.MODULE, 'io.github.nku100.impad')
        self.assertEqual(strings['app_name'], "I\\'m Pad")
        self.assertEqual(strings['app_description'], '微信、QQ、企业微信的平板登录适配模块')
        self.assertEqual((root / 'app/src/main/resources/META-INF/xposed/java_init.list').read_text().strip(),
                         'io.github.nku100.impad.ImPadModule')
        self.assertEqual((root / 'app/src/main/resources/META-INF/xposed/scope.list').read_text().splitlines(),
                         ['com.tencent.mm', 'com.tencent.wework', 'com.tencent.mobileqq'])

    def test_script_help_imports_from_clean_environment(self):
        environment = os.environ.copy()
        environment.pop('PYTHONPATH', None)
        result = subprocess.run(
            [sys.executable, 'scripts/ci/runtime_smoke.py', '--help'],
            cwd=Path(__file__).resolve().parents[1],
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('--app-id APP_ID', result.stdout)

    def test_candidate_abi_is_read_from_apk_and_rejects_mixed_or_unsupported_native_abis(self):
        with tempfile.TemporaryDirectory() as temporary:
            apk = Path(temporary) / 'candidate.apk'
            with zipfile.ZipFile(apk, 'w') as archive:
                archive.writestr('lib/arm64-v8a/libmodule.so', b'arm64')
            self.assertEqual('arm64-v8a', smoke.apk_native_abi(apk))
            with zipfile.ZipFile(apk, 'w') as archive:
                archive.writestr('lib/x86_64/libmodule.so', b'x86')
            with self.assertRaisesRegex(ValueError, 'unsupported'):
                smoke.apk_native_abi(apk)
            with zipfile.ZipFile(apk, 'w') as archive:
                archive.writestr('lib/arm64-v8a/libmodule.so', b'arm64')
                archive.writestr('lib/x86_64/libmodule.so', b'x86')
            with self.assertRaisesRegex(ValueError, 'exactly one'):
                smoke.apk_native_abi(apk)

    def test_page_size_backcompat_is_enabled_before_each_launch(self):
        with patch.object(smoke, "su") as root:
            smoke.enable_page_size_backcompat()
        self.assertEqual(root.call_args_list[0].args, ("setprop bionic.linker.16kb.app_compat.enabled true; setprop pm.16kb.app_compat.disabled false",))

    def test_snapshot_uses_native_hierarchy_reader(self):
        result = subprocess.CompletedProcess([], 0, b"<hierarchy />", b"")
        with tempfile.TemporaryDirectory() as folder, patch.object(smoke, "EVIDENCE", Path(folder)), patch.object(smoke, "adb", return_value=result) as device:
            smoke.snapshot("probe")
        self.assertTrue(any("UiHierarchy" in call.args for call in device.call_args_list))
        self.assertFalse(any("/sdcard/" in str(call.args) for call in device.call_args_list))

    def test_hidden_nodes_are_not_click_targets(self):
        xml = '<hierarchy><node text="Modules" visible="false"/><node text="Modules" visible="true" bounds="[0,0][10,10]"/></hierarchy>'
        self.assertEqual(smoke.unique_node(xml, lambda n: n.get("text") == "Modules").get("visible"), "true")

    def test_ocr_target_requires_unique_confident_text(self):
        tsv = 'level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n'
        line = '5\t1\t1\t1\t1\t1\t50\t70\t140\t30\t96\tImPad\n'
        target = smoke.ocr_target(tsv + line, 'ImPad')
        self.assertEqual(target.get('bounds'), '[50,70][190,100]')
        self.assertIsNone(smoke.ocr_target(tsv + line.replace('96', '20'), 'ImPad'))
        with self.assertRaisesRegex(ValueError, 'ambiguous'):
            smoke.ocr_target(tsv + line + line.replace('5\t1\t1\t1\t1\t1', '5\t1\t2\t1\t1\t1'), 'ImPad')

    def test_ocr_target_matches_multiword_module_label_with_apostrophe(self):
        tsv = 'level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n'
        line = (
            '5\t1\t1\t1\t1\t1\t20\t40\t12\t24\t95\tI\n'
            '5\t1\t1\t1\t1\t2\t34\t40\t25\t24\t93\tm\n'
            '5\t1\t1\t1\t1\t3\t65\t40\t48\t24\t96\tPad\n'
        )
        target = smoke.ocr_target(tsv + line, smoke.MODULE_LABEL)
        self.assertEqual(target.get('text'), 'I m Pad')
        self.assertEqual(target.get('bounds'), '[20,40][113,64]')

    def test_tablet_only_selector_is_navigation_not_final_success(self):
        ui = '<hierarchy><node package="com.tencent.mm" text="Log in on Tablet Only" bounds="[0,0][100,100]" /></hierarchy>'
        mobile = '<hierarchy><node package="com.tencent.mm" text="Mobile Number" /></hierarchy>'
        observations = [('LoginSelectUI', ui), ('MobileInputUI', mobile), ('MobileInputUI', mobile), ('MobileInputUI', mobile)]
        with patch.object(smoke, 'enable_page_size_backcompat'), patch.object(smoke, 'adb'), patch.object(smoke, 'launcher_component', return_value='com.tencent.mm/.ui.LauncherUI'), patch.object(smoke, 'snapshot', side_effect=observations), patch.object(smoke, 'tap') as click, patch.object(smoke.time, 'sleep'):
            self.assertEqual(smoke.mobile_input('module'), mobile)
            self.assertEqual(click.call_count, 1)

    def test_launcher_is_not_wechat(self):
        self.assertFalse(smoke.has_wechat_ui('<hierarchy><node package="com.google.android.apps.nexuslauncher" /></hierarchy>'))

    def test_wechat_ui_is_recognized(self):
        self.assertTrue(smoke.has_wechat_ui('<hierarchy><node package="com.tencent.mm" /></hierarchy>'))

    def test_tablet_entry_requires_explicit_login_choice(self):
        self.assertFalse(smoke.is_tablet_entry('Use a tablet to read more'))
        self.assertFalse(smoke.is_tablet_entry('Log in on Tablet Only'))
        self.assertTrue(smoke.is_tablet_entry('Logged in on Phone & Tablet'))

    def test_qr_requires_activity_and_rendered_wechat_instructions(self):
        ui = '<hierarchy><node package="com.tencent.mm" text="Scan the QR code with WeChat" /></hierarchy>'
        self.assertFalse(smoke.qr_page_ready('topResumedActivity=com.tencent.mm/.app.WeChatSplashActivity', ui))
        self.assertTrue(smoke.qr_page_ready('topResumedActivity=com.tencent.mm/.plugin.account.ui.LoginAsExDeviceUI', ui))
        self.assertFalse(smoke.qr_page_ready('topResumedActivity=com.tencent.mm/.plugin.account.ui.LoginAsExDeviceUI', '<hierarchy />'))

    def test_click_target_must_be_unique(self):
        xml = '<hierarchy><node text="Modules" bounds="[0,0][10,10]"/><node text="Modules" bounds="[10,10][20,20]"/></hierarchy>'
        with self.assertRaises(ValueError):
            smoke.unique_node(xml, lambda node: node.get('text') == 'Modules')


if __name__ == '__main__':
    unittest.main()
