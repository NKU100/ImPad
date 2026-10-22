import unittest
from dataclasses import replace
import json
from pathlib import Path
import subprocess
import tempfile
from unittest.mock import patch

from scripts.ci.app_policy import POLICIES, get_policy, policy_for_report, validate_rule_changes, _catalog_at_revision
from scripts.ci.codex_adaptation import validate_paths


class AppPolicyTest(unittest.TestCase):
    def test_revision_catalog_reads_checker_json_from_the_archived_tree(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._guard_repository(root)
            catalog = dict(self._catalog('wecom'), schemaVersion=1)
            (root / 'catalog-fixture.json').write_text(json.dumps(catalog))
            wrapper = root / 'gradlew'
            wrapper.write_text(
                '#!/usr/bin/env python3\n'
                'import json, shlex, sys\n'
                'from pathlib import Path\n'
                'assert sys.argv[1:4] == ["--offline", "--no-daemon", ":compat-checker:run"]\n'
                'arguments = shlex.split(sys.argv[4].removeprefix("--args="))\n'
                'assert arguments[:3] == ["catalog", "--app-id", "wecom"]\n'
                'assert arguments[3] == "--report"\n'
                'Path(arguments[4]).write_text(Path("catalog-fixture.json").read_text())\n'
            )
            wrapper.chmod(0o755)
            subprocess.run(['git', 'add', '.'], cwd=root, check=True)
            subprocess.run(['git', 'commit', '-qm', 'checker report fixture'], cwd=root, check=True)
            revision = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, text=True).strip()
            self.assertEqual(catalog, _catalog_at_revision(root, revision, 'wecom'))

    def test_registered_app_policies_have_no_legacy_mode_or_targets_path(self):
        self.assertEqual(set(POLICIES), {"wechat", "qq", "wecom"})
        for app_id in ("wechat", "qq", "wecom"):
            with self.subTest(app_id=app_id):
                policy = get_policy(app_id)
                self.assertFalse(hasattr(policy, "resolution_mode"))
                self.assertFalse(hasattr(policy, "targets_path"))
        with self.assertRaisesRegex(ValueError, "Unknown or unregistered appId"):
            get_policy(None)

    def test_checker_entrypoints_are_only_shared_rules_and_catalog(self):
        main = Path("compat-checker/src/main/kotlin/io/github/nku100/impad/checker/Main.kt").read_text()
        self.assertNotIn('"check" ->', main)
        self.assertNotIn('"analyze-candidate" ->', main)
        self.assertIn('"check-rules" ->', main)
        self.assertIn('"catalog" ->', main)

    def test_old_descriptor_result_model_is_not_part_of_the_rules_runtime(self):
        self.assertFalse(Path('compat-core/src/main/kotlin/io/github/nku100/impad/compat/CompatibilityResult.kt').exists())

    @staticmethod
    def _catalog(app_id, strategy_id=None):
        rule_id = f'{app_id}_rule_001'
        strategy_id = strategy_id or f'{app_id}_strategy_001'
        return {'appId': app_id,
                'definitions': [{'ruleId': rule_id, 'strategyId': strategy_id,
                                 'requiredTargetIds': ['device_type']}],
                'bindings': [{'ruleId': rule_id, 'min': 1, 'max': None}],
                'strategyIds': [strategy_id]}

    def _guard_repository(self, root):
        subprocess.run(['git', 'init', '-q'], cwd=root, check=True)
        subprocess.run(['git', 'config', 'user.name', 'Task7 Test'], cwd=root, check=True)
        subprocess.run(['git', 'config', 'user.email', 'task7@example.invalid'], cwd=root, check=True)
        sources = {}
        records = {}
        for app_id in ('qq', 'wecom'):
            relative = Path('compat-rules/src/main/kotlin/io/github/nku100/impad/rules') / app_id / f'{app_id.title()}ExistingRule.kt'
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f'package rules.{app_id}\nobject ExistingRule {{ const val RULE = "{app_id}_rule_001" }}\n')
            sources[app_id] = relative.as_posix()
            (root / f'compatibility/{app_id}').mkdir(parents=True, exist_ok=True)
            (root / f'compatibility/{app_id}/acceptance.json').write_text('{}\n')
            (root / f'compatibility/{app_id}/targets.json').write_text('[]\n')
            records[app_id] = [{'module': {'fingerprint': {'moduleSources': [
                {'path': relative.as_posix(), 'sha256': __import__('hashlib').sha256(path.read_bytes()).hexdigest()},
            ]}}}]
        shared = root / 'compat-rules/src/main/kotlin/io/github/nku100/impad/rules/RuleCatalog.kt'
        shared.parent.mkdir(parents=True, exist_ok=True)
        shared.write_text('object RuleCatalog { fun forApp(id: String) = id }\n')
        subprocess.run(['git', 'add', '.'], cwd=root, check=True)
        subprocess.run(['git', 'commit', '-qm', 'trusted base'], cwd=root, check=True)
        base = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, text=True).strip()
        return sources, base, records

    def test_rule_guard_rejects_mutating_existing_application_rule_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sources, base, records = self._guard_repository(root)
            path = root / sources['qq']
            path.write_text(path.read_text().replace('qq_rule_001', 'qq_rule_changed'))
            subprocess.run(['git', 'add', '.'], cwd=root, check=True)
            subprocess.run(['git', 'commit', '-qm', 'mutated old rule'], cwd=root, check=True)
            head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, text=True).strip()
            with patch('scripts.ci.acceptance.load_records_bytes', side_effect=lambda raw, app, *args, **kwargs: records[app]), \
                 patch('scripts.ci.app_policy._catalog_at_revision', side_effect=lambda _repo, _rev, app: self._catalog(app)):
                with self.assertRaisesRegex(ValueError, 'immutable|existing rule|strategy'):
                    validate_rule_changes(base, head, repository=root)

    def test_rule_guard_allows_additive_rule_file_and_registry_extension(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, base, records = self._guard_repository(root)
            new_rule = root / 'compat-rules/src/main/kotlin/io/github/nku100/impad/rules/qq/QqNewRule.kt'
            new_rule.write_text('package rules.qq\nobject QqNewRule { const val RULE = "qq_rule_002" }\n')
            shared = root / 'compat-rules/src/main/kotlin/io/github/nku100/impad/rules/RuleCatalog.kt'
            shared.write_text(shared.read_text().replace('id }', 'id /* additive qq_rule_002 */ }'))
            subprocess.run(['git', 'add', '.'], cwd=root, check=True)
            subprocess.run(['git', 'commit', '-qm', 'add rule'], cwd=root, check=True)
            head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, text=True).strip()
            with patch('scripts.ci.acceptance.load_records_bytes', side_effect=lambda raw, app, *args, **kwargs: records[app]), \
                 patch('scripts.ci.app_policy._catalog_at_revision', side_effect=lambda _repo, _rev, app: self._catalog(app)):
                validate_rule_changes(base, head, repository=root)

    def test_rule_guard_rejects_changing_an_existing_catalog_definition(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, base, records = self._guard_repository(root)
            shared = root / 'compat-rules/src/main/kotlin/io/github/nku100/impad/rules/RuleCatalog.kt'
            shared.write_text(shared.read_text() + '\n// changed old qq strategy definition\n')
            subprocess.run(['git', 'add', '.'], cwd=root, check=True)
            subprocess.run(['git', 'commit', '-qm', 'change old catalog definition'], cwd=root, check=True)
            head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, text=True).strip()
            catalogs = {base: {'qq': self._catalog('qq'), 'wecom': self._catalog('wecom')},
                        head: {'qq': self._catalog('qq', 'qq_strategy_changed'),
                               'wecom': self._catalog('wecom')}}
            with patch('scripts.ci.acceptance.load_records_bytes', side_effect=lambda raw, app, *args, **kwargs: records[app]), \
                 patch('scripts.ci.app_policy._catalog_at_revision', side_effect=lambda _repo, revision, app: catalogs[revision][app]):
                with self.assertRaisesRegex(ValueError, 'Existing rule catalog definitions are immutable'):
                    validate_rule_changes(base, head, repository=root)

    def test_adaptation_rejects_checker_changes_for_every_app(self):
        for app_id in ('wechat', 'wecom', 'qq'):
            with self.subTest(app_id=app_id), self.assertRaisesRegex(ValueError, 'outside the allowed'):
                validate_paths(['compat-checker/src/main/kotlin/io/github/nku100/impad/checker/Main.kt'], app_id)

    def test_wechat_allowlist_uses_impad_package_paths(self):
        policy = get_policy('wechat')
        allowed = [
            policy.app_source_prefix + 'WeChatAppAdapter.kt',
            'app/src/main/kotlin/io/github/nku100/impad/ImPadModule.kt',
        ]
        validate_paths(allowed, 'wechat')
        with self.assertRaisesRegex(ValueError, 'outside the allowed'):
            validate_paths(['app/src/main/kotlin/io/github/nku100/wechatpad/Legacy.kt'], 'wechat')

    def test_adaptation_allows_additive_rule_sources_but_not_historical_targets(self):
        validate_paths([
            'compat-rules/src/main/kotlin/io/github/nku100/impad/rules/qq/QqNextRule.kt',
            'compat-rules/src/test/kotlin/io/github/nku100/impad/rules/qq/QqNextRuleTest.kt',
            'compat-rules/src/main/kotlin/io/github/nku100/impad/rules/RuleCatalog.kt',
        ], 'qq')
        with self.assertRaisesRegex(ValueError, 'outside the allowed'):
            validate_paths(['compatibility/qq/targets.json'], 'qq')

    def test_unknown_explicit_app_never_falls_back_to_wechat(self):
        with self.assertRaisesRegex(ValueError, "Unknown or unregistered appId"):
            get_policy("unregistered")
        with self.assertRaisesRegex(ValueError, "Unknown or unregistered appId"):
            policy_for_report({"identity": {"packageName": "com.tencent.mm"}}, "unregistered")

    def test_wecom_runtime_policy_is_separate_from_wechat(self):
        wechat = get_policy("wechat")
        wecom = get_policy("wecom")
        self.assertEqual(wecom.package_name, "com.tencent.wework")
        self.assertEqual(wecom.required_hook_ids, ("device_type", "app_pad", "service_pad"))
        self.assertEqual(wecom.runtime_baseline, "NO_TABLET_ENTRY")
        self.assertEqual(wecom.runtime_qr_page, "LoginQrCodeForAndroidPad")
        self.assertNotEqual(wecom.runtime_ui_strategy, wechat.runtime_ui_strategy)
        with self.assertRaisesRegex(ValueError, "package"):
            policy_for_report({"appId": "wecom", "identity": {"packageName": "com.tencent.mm"}}, "wecom")

    def test_wecom_policy_preserves_its_dedicated_rule_configuration(self):
        policy = get_policy("wecom")
        self.assertTrue(Path(policy.adaptation_prompt).is_file(), policy.adaptation_prompt)
        self.assertEqual(policy.required_hook_ids, ("device_type", "app_pad", "service_pad"))

    def test_test_only_second_policy_is_confined_to_its_own_paths(self):
        test_policy = replace(
            get_policy("wechat"), app_id="testapp", package_name="org.example.testapp",
            app_source_prefix="app/src/main/kotlin/example/testapp/",
            shared_source_paths=(), artifact_prefix="test-only",
        )
        with patch.dict(POLICIES, {"testapp": test_policy}):
            validate_paths(["app/src/main/kotlin/example/testapp/Adapter.kt"], "testapp")
            for path in ["compatibility/testapp/targets.json", "compatibility/wechat/targets.json",
                         "app/src/main/kotlin/io/github/nku100/impad/apps/wechat/Adapter.kt"]:
                with self.subTest(path=path), self.assertRaisesRegex(ValueError, "outside the allowed"):
                    validate_paths([path], "testapp")


if __name__ == "__main__":
    unittest.main()
