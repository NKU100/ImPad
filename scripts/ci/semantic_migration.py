"""Frozen v1 history and explicit v2 expectations, never runtime acceptance."""
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path

from scripts.ci.acceptance import load_records_bytes, select_representatives, validate_expected_plan
from scripts.ci.semantic_protocol import (plan_execution_sha256, validate_catalog,
    validate_check_report, validate_complete_plan, validate_snapshot)


@dataclass(frozen=True)
class SemanticMigrationBundle:
    history_sha256: str
    expectations_sha256: str
    profiles_json: bytes
    historical_records_json: bytes

    @property
    def profiles(self):
        return json.loads(self.profiles_json)

    @property
    def historical_records(self):
        return json.loads(self.historical_records_json)


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _within(repository, relative):
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute() or '..' in Path(relative).parts:
        raise ValueError('Migration reference must be relative to the repository')
    path = (repository / relative).resolve()
    if not path.is_relative_to(repository):
        raise ValueError('Migration reference escapes the repository')
    return path


def _pinned_json(repository, reference):
    if not isinstance(reference, dict) or set(reference) != {'path', 'sha256'}:
        raise ValueError('Invalid migration evidence reference')
    raw = _within(repository, reference['path']).read_bytes()
    if _sha(raw) != reference['sha256']:
        raise ValueError('Pinned migration evidence bytes changed')
    return json.loads(raw)


def legacy_catalog_sha256(catalog):
    text = catalog['appId'] + '\n'
    for row in catalog['definitions']:
        text += row['ruleId'] + '|' + row['strategyId'] + '|' + ','.join(sorted(row['requiredTargetIds'])) + '\n'
    for row in catalog['bindings']:
        text += row['ruleId'] + '|' + str(row['min']) + '|' + ('∞' if row['max'] is None else str(row['max'])) + '\n'
    text += ''.join('strategy|' + value + '\n' for value in sorted(catalog['strategyIds']))
    return _sha(text.encode('utf-8'))


def _load_history(repository, history):
    if not isinstance(history, dict) or set(history) != {'schemaVersion', 'purpose', 'tables'} or history['schemaVersion'] != 1 or history['purpose'] != 'HISTORICAL_CATALOG_AUDIT':
        raise ValueError('Invalid historical catalog audit')
    if not isinstance(history['tables'], list) or {row['appId'] for row in history['tables']} != {'wechat', 'qq', 'wecom'} or len(history['tables']) != 3:
        raise ValueError('Historical audit must preserve all three applications')
    records, representatives, table_hashes = {}, set(), {}
    for table in history['tables']:
        if set(table) != {'appId', 'path', 'sha256', 'records'} or table['path'] != f"compatibility/{table['appId']}/acceptance.json":
            raise ValueError('Invalid historical acceptance reference')
        raw = _within(repository, table['path']).read_bytes()
        if _sha(raw) != table['sha256']:
            raise ValueError('Historical acceptance bytes changed')
        table_hashes[table['appId']] = table['sha256']
        if not isinstance(table['records'], list) or not table['records']:
            raise ValueError('Historical audit has no records')
        catalogs = {}
        for row in table['records']:
            if set(row) != {'recordId', 'catalogSha256', 'catalog', 'provenance'} or row['recordId'] in catalogs:
                raise ValueError('Invalid or duplicate historical record audit')
            catalog = _pinned_json(repository, row['catalog'])
            if (catalog.get('schemaVersion') != 1 or catalog.get('appId') != table['appId']
                    or catalog.get('ruleCatalogSha256') != row['catalogSha256']
                    or legacy_catalog_sha256(catalog) != row['catalogSha256']):
                raise ValueError('Historical catalog differs from its original content identity')
            catalogs[row['recordId']] = catalog
        # The last historical catalog validates the retained v1 table envelope;
        # each record is then checked against its own original catalog below.
        rows = load_records_bytes(raw, table['appId'], False, catalog=list(catalogs.values())[-1],
            repository=repository, source_path=_within(repository, table['path']))
        if {row['recordId'] for row in rows} != set(catalogs):
            raise ValueError('Historical audit omitted or invented a record')
        for row in rows:
            catalog = catalogs[row['recordId']]
            if row['ruleCatalogSha256'] != catalog['ruleCatalogSha256']:
                raise ValueError('Historical record catalog pin differs')
            validate_expected_plan(row['expectedPlan'], catalog, row['identity']['versionCode'])
            key = table['appId'], row['recordId']
            records[key] = row
        representatives.update((table['appId'], row['recordId']) for row in select_representatives(rows))
    return records, representatives, table_hashes


def _native_plan(row):
    plan = json.loads(json.dumps(row['plan']))
    digest = plan.pop('executionSha256')
    plan['schemaVersion'] = 1
    for hook in plan['hooks']:
        hook.pop('boundMethod')
    if plan_execution_sha256(plan) != digest:
        raise ValueError('Native analysis execution digest differs')
    return plan, digest


def load_migration_bundle(repository):
    repository = Path(repository).resolve()
    try:
        history_raw = (repository / 'compatibility/semantic-migration/history.json').read_bytes()
        expected_raw = (repository / 'compatibility/semantic-migration/expectations.json').read_bytes()
        history, expected = json.loads(history_raw), json.loads(expected_raw)
        records, representatives, table_hashes = _load_history(repository, history)
        if (not isinstance(expected, dict) or set(expected) != {'schemaVersion', 'purpose', 'evidenceScope', 'analysisEvidence', 'profiles'}
                or expected['schemaVersion'] != 1 or expected['purpose'] != 'FIXED_REGRESSION_EXPECTATIONS'
                or expected['evidenceScope'] != 'CODE_PARSE_REFLECTION_ONLY' or not isinstance(expected['profiles'], list)):
            raise ValueError('Migration expectations are not an explicit analysis-only schema')
        analysis = _pinned_json(repository, expected['analysisEvidence'])
        if analysis.get('schemaVersion') != 1 or analysis.get('scope') != 'REAL_DEX_PARSE_REFLECTION_PROBE':
            raise ValueError('Migration source is not the pinned native parsing experiment')
        profiles, profile_ids = set(), set()
        for profile in expected['profiles']:
            if set(profile) != {'profileId', 'appId', 'historicalRecordId', 'historicalAcceptanceSha256', 'identity', 'apkInputSha256', 'ruleCatalog', 'snapshot', 'expectedPlan', 'expectedExecutionSha256', 'analysisCaseName'}:
                raise ValueError('Invalid frozen semantic expectation profile')
            key = profile['appId'], profile['historicalRecordId']
            if key not in representatives or key in profiles or profile['profileId'] in profile_ids:
                raise ValueError('Semantic migration must cover each fixed representative exactly once')
            profiles.add(key); profile_ids.add(profile['profileId'])
            old = records[key]
            if profile['historicalAcceptanceSha256'] != table_hashes[profile['appId']] or profile['identity'] != old['identity']:
                raise ValueError('Migration profile does not preserve the original accepted input identity')
            if profile['apkInputSha256'] != [row['sha256'].lower() for row in old['source']['apkFiles']]:
                raise ValueError('Migration profile changed fixed base/split input bytes')
            validate_catalog(profile['ruleCatalog'], profile['appId'])
            validate_snapshot(profile['snapshot'], [{'path': f'input-{index}.apk', 'sha256': digest} for index, digest in enumerate(profile['apkInputSha256'])])
            digest = validate_complete_plan(profile['expectedPlan'], profile['ruleCatalog'], profile['snapshot'])
            if digest != profile['expectedExecutionSha256']:
                raise ValueError('Frozen complete expectation execution digest differs')
            matches = [row for row in analysis['reports'] if row['caseName'] == profile['analysisCaseName'] and row['appId'] == profile['appId']]
            if len(matches) != 1 or matches[0]['status'] != 'Selected' or matches[0]['scope'] != 'REAL_DEX_PARSE_REFLECTION_PROBE':
                raise ValueError('Frozen expectation has no exact native analysis case')
            native_plan, native_digest = _native_plan(matches[0])
            if native_digest != digest or native_plan != profile['expectedPlan']:
                raise ValueError('Frozen expectation differs from the independently recorded code analysis')
        if profiles != representatives:
            raise ValueError('Semantic migration omitted fixed acceptance representatives')
        return SemanticMigrationBundle(_sha(history_raw), _sha(expected_raw),
            json.dumps(expected['profiles'], separators=(',', ':')).encode(),
            json.dumps(list(records.values()), separators=(',', ':')).encode())
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as error:
        raise ValueError('Semantic migration artifacts are missing or malformed') from error


def compare_fixed_report(bundle, historical_record_id, report):
    if not isinstance(bundle, SemanticMigrationBundle):
        raise ValueError('Expected an immutable semantic migration snapshot')
    matches = [row for row in bundle.profiles if row['historicalRecordId'] == historical_record_id]
    if len(matches) != 1:
        raise ValueError('No unique frozen expectation for the historical record')
    profile = matches[0]
    validate_check_report(report, expected_app_id=profile['appId'], expected_identity=profile['identity'], trusted_catalog=profile['ruleCatalog'])
    if (report['selection']['status'] != 'SELECTED' or report['snapshot'] != profile['snapshot']
            or report['selection']['plan'] != profile['expectedPlan']
            or report['selection']['executionSha256'] != profile['expectedExecutionSha256']
            or [row['sha256'].lower() for row in report['inputHashes']] != profile['apkInputSha256']):
        raise ValueError('Regression selection differs from its frozen complete semantic expectation')
