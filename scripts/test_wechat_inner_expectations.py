from scripts.testdata.acceptance_baseline import copy_migration_baseline
import copy
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from scripts.ci.semantic_migration import load_migration_bundle
from scripts.ci.wechat_inner_expectations import project_wechat_profiles
from scripts.ci.semantic_protocol import plan_execution_sha256

ROOT = Path(__file__).resolve().parents[1]
class InnerAfterExpectationsTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        copy_migration_baseline(ROOT/'compatibility', self.root/'compatibility')
        self.bundle=load_migration_bundle(self.root)
    def test_projects_all_frozen_wechat_profiles_without_mutating_history(self):
        before={str(p.relative_to(self.root)):p.read_bytes() for p in (self.root/'compatibility/semantic-migration').rglob('*') if p.is_file()}
        digest,profiles=project_wechat_profiles(self.root,self.bundle)
        self.assertEqual(64,len(digest));self.assertEqual(2,len(profiles))
        for p in profiles.values():
            self.assertEqual(['inner','tablet','login'],[h['role'] for h in p['expectedPlan']['hooks']])
            self.assertEqual(p['expectedExecutionSha256'],plan_execution_sha256(p['expectedPlan']))
            self.assertNotIn('booleanCacheInitializations',p['expectedPlan'])
        self.assertEqual(before,{str(p.relative_to(self.root)):p.read_bytes() for p in (self.root/'compatibility/semantic-migration').rglob('*') if p.is_file()})
    def test_rejects_missing_duplicate_wrong_source_and_foreign_field(self):
        path=self.root/'compatibility/wechat/inner-after-audit.json';original=json.loads(path.read_text())
        changes=[]
        d=copy.deepcopy(original);d['profiles']=d['profiles'][:1];changes.append(d)
        d=copy.deepcopy(original);d['profiles'][1]=d['profiles'][0];changes.append(d)
        d=copy.deepcopy(original);d['profiles'][0]['source']['dexSha256']='a'*64;changes.append(d)
        d=copy.deepcopy(original);d['profiles'][0]['cacheField']='Lother/Host;->cache:Ljava/lang/Boolean;';changes.append(d)
        for d in changes:
            path.write_text(json.dumps(d))
            with self.assertRaises(ValueError):project_wechat_profiles(self.root,self.bundle)
    def test_audit_change_invalidates_baseline_even_when_no_v2_table_exists(self):
        from scripts.ci.semantic_regression_baseline import load_semantic_regression_baseline
        baseline=load_semantic_regression_baseline(self.root,'wechat')
        path=self.root/'compatibility/wechat/inner-after-audit.json';data=json.loads(path.read_text());data['profiles'][0]['readCount']+=1;path.write_text(json.dumps(data))
        with self.assertRaises(ValueError):baseline.assert_unchanged()
    def test_unmodified_qq_baseline_uses_frozen_catalog(self):
        from scripts.ci.semantic_regression_baseline import load_semantic_regression_baseline
        cases=load_semantic_regression_baseline(self.root,'qq').cases
        self.assertTrue(all('innerAfterAuditSha256' not in case for case in cases))
