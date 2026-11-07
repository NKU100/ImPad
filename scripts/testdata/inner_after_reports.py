"""Synthetic checker reports using the current audited contract; never runtime evidence."""
from pathlib import Path
from scripts.test_semantic_migration import SemanticMigrationTest as HistoricalReportFixture, ROOT
from scripts.ci.semantic_migration import load_migration_bundle
from scripts.ci.wechat_inner_expectations import project_wechat_profiles


def current_profiles(bundle, repository=ROOT):
    _, projected = project_wechat_profiles(Path(repository), bundle)
    return [projected.get(p['historicalRecordId'], p) for p in bundle.profiles]


class CurrentReportFixture:
    def __init__(self, *args):
        pass
    def report(self, profile):
        bundle = load_migration_bundle(ROOT)
        _, projected = project_wechat_profiles(ROOT, bundle)
        return HistoricalReportFixture.report(HistoricalReportFixture('runTest'), projected.get(profile['historicalRecordId'], profile))
