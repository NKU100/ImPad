"""Audited static projection of frozen hook plans onto the inner-after contract."""
import copy
import hashlib
import json
from pathlib import Path
from scripts.ci.semantic_protocol import catalog_sha256, validate_complete_plan


def project_wechat_profiles(repository, bundle):
    root = Path(repository).resolve()
    path = root / 'compatibility/wechat/inner-after-audit.json'
    if not path.exists():
        return None, {}
    if path.is_symlink() or any(p.is_symlink() for p in path.parents if p.is_relative_to(root)):
        raise ValueError('Inner-after audit must not be a symlink')
    raw = path.read_bytes()
    audit = json.loads(raw)
    if set(audit) != {'schemaVersion', 'scope', 'migrationExpectationsSha256', 'profiles'} or audit['schemaVersion'] != 1 or audit['scope'] != 'STATIC_DEX_RELATIONSHIP_ONLY' or audit['migrationExpectationsSha256'] != bundle.expectations_sha256:
        raise ValueError('Inner-after audit differs from frozen migration source')
    old = {p['profileId']: p for p in bundle.profiles if p['appId'] == 'wechat'}
    records = audit['profiles']
    if not isinstance(records, list) or len(records) != len(old) or {p['profileId'] for p in records} != set(old):
        raise ValueError('Inner-after audit must cover all frozen WeChat profiles')
    result = {}
    for record in records:
        if set(record) != {'profileId', 'apkInputSha256', 'cacheField', 'monitorField', 'decisionDescriptor', 'decisionShape', 'source', 'readCount', 'writeCount', 'cacheAccessFlags', 'expectedExecutionSha256'}:
            raise ValueError('Invalid inner-after audit record')
        profile = copy.deepcopy(old[record['profileId']])
        tablet = next(h for h in profile['expectedPlan']['hooks'] if h['role'] == 'tablet')
        if record['apkInputSha256'] != profile['apkInputSha256'] or record['source'] != tablet['source'] or any(type(record[k]) is not int or record[k] <= 0 for k in ['readCount', 'writeCount']):
            raise ValueError('Inner-after audit differs from frozen DEX identity')
        flags = record['cacheAccessFlags']
        if type(flags) is not int or not flags & 8 or flags & 16:
            raise ValueError('Audited cache must be writable and static')
        owner = tablet['descriptor'].split('->')[0]
        if not record['decisionDescriptor'].startswith(owner + '->'):
            raise ValueError('Inner decision owner differs from wrapper')
        config = {'policy': 'cache_true_after', 'cacheField': record['cacheField'], 'monitorField': record['monitorField']}
        catalog = profile['ruleCatalog']
        catalog['contractRevision'] = 2
        catalog['requiredRoles'] = ['inner', 'login', 'tablet']
        for strategy in catalog['strategies']:
            strategy['revision'] = 2
            strategy['roles'].insert(0, {'role': 'inner', 'phase': 'AFTER', 'configuration': {'policy': 'cache_true_after'}})
        catalog['ruleCatalogSha256'] = catalog_sha256(catalog)
        plan = profile['expectedPlan']
        plan['contractRevision'] = 2
        plan['strategyRevision'] = 2
        plan['hooks'].insert(0, {'role': 'inner', 'descriptor': record['decisionDescriptor'], 'shape': record['decisionShape'], 'source': record['source'], 'phase': 'AFTER', 'configuration': config})
        profile['expectedExecutionSha256'] = validate_complete_plan(plan, catalog, profile['snapshot'])
        if profile['expectedExecutionSha256'] != record['expectedExecutionSha256']:
            raise ValueError('Inner-after projection differs from independently audited execution digest')
        result[profile['historicalRecordId']] = profile
    return hashlib.sha256(raw).hexdigest(), result
