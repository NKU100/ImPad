import io
import json
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlsplit

from scripts.ci.app_policy import get_policy
from scripts.ci.apps.qq import discovery_policy
from scripts.ci.apps.qq.discovery_policy import discover_from_config
from scripts.ci.apps.qq.runtime_policy import validate_request_evidence
from scripts.test_qq_requests import proof, expectation


APK_PREFIX = 'https://downv6.qq.com/qqweb/QQ_1/android_apk/'


def qq_config(version, filename):
    return {'android': {'version': version, 'x64Link': APK_PREFIX + filename}}


STALE = qq_config('9.3.70', '9.3.70.41925_2a98ecf56e5ef04b.apk')
OLDER = qq_config('9.3.75', '9.3.75.42350_8b979832d5b7a366.apk')
LATEST = qq_config('9.3.80', '9.3.80.42775_4bae4d0ebb0636b7.apk')


def doh_response(payload):
    response = MagicMock()
    response.__enter__.return_value.read.return_value = json.dumps(payload).encode()
    return response


class QqPolicyTest(unittest.TestCase):
    def test_login_activity_without_visible_inputs_is_not_ready(self):
        from scripts.ci.apps.qq.runtime_policy import login_input_ready
        package = 'com.tencent.mobileqq'
        activity = 'com.tencent.mobileqq/.activity.LoginActivity'
        welcome = '<hierarchy><node package="com.tencent.mobileqq" class="android.widget.Button" text="登录"/></hierarchy>'
        self.assertFalse(login_input_ready(activity, welcome, package))
        for visible, expected in [('false', False), ('true', True)]:
            xml = '<hierarchy><node package="com.tencent.mobileqq" class="android.widget.EditText" visible="' + visible + '" bounds="[0,10][100,50]"/></hierarchy>'
            self.assertEqual(expected, login_input_ready(activity, xml, package))

    def test_official_config_selects_arm64_download(self):
        config = {'android': {'version': '9.3.70', 'x64Link':
            'https://downv6.qq.com/qqweb/QQ_1/android_apk/9.3.70.41925_2a98ecf56e5ef04b.apk',
            'x32Link': 'https://example.org/wrong.apk'}}
        result = discover_from_config(config, get_policy('qq').official_apk_prefix)
        self.assertEqual(result.version_code, 41925)
        with self.assertRaises(ValueError):
            discover_from_config({'android': dict(config['android'], x64Link='https://example.org/qq.apk')},
                                 get_policy('qq').official_apk_prefix)
        with self.assertRaises(ValueError):
            discover_from_config({'android': dict(config['android'], version='9.3.75')},
                                 get_policy('qq').official_apk_prefix)
        with self.assertRaises(ValueError):
            discover_from_config({'android': []}, get_policy('qq').official_apk_prefix)

    def test_discovery_selects_highest_release_across_default_and_mainland_edges(self):
        edges = {'192.0.2.10': OLDER, '192.0.2.11': LATEST}

        def edge_config(address):
            if address == '192.0.2.12':
                raise OSError('edge unreachable')
            return edges[address]

        with patch.object(discovery_policy, 'fetch_config', return_value=STALE), \
                patch.object(discovery_policy, 'mainland_edge_addresses',
                             return_value=['192.0.2.10', '192.0.2.11', '192.0.2.12']), \
                patch.object(discovery_policy, 'fetch_config_from_edge', side_effect=edge_config), \
                patch('sys.stderr', io.StringIO()) as log:
            candidate = discovery_policy.discover(get_policy('qq'))
        self.assertEqual(candidate.filename, '9.3.80.42775_4bae4d0ebb0636b7.apk')
        self.assertEqual(candidate.version_code, 42775)
        self.assertIn('default resolution: 9.3.70.41925', log.getvalue())
        self.assertIn('mainland edge 192.0.2.12 failed: edge unreachable', log.getvalue())

    def test_discovery_uses_default_resolution_when_no_mainland_edge_is_available(self):
        with patch.object(discovery_policy, 'fetch_config', return_value=STALE), \
                patch.object(discovery_policy, 'mainland_edge_addresses', return_value=[]), \
                patch('sys.stderr', io.StringIO()):
            candidate = discovery_policy.discover(get_policy('qq'))
        self.assertEqual(candidate.version_code, 41925)

    def test_discovery_uses_mainland_edge_when_default_resolution_fails(self):
        with patch.object(discovery_policy, 'fetch_config', side_effect=TimeoutError('handshake timed out')), \
                patch.object(discovery_policy, 'mainland_edge_addresses', return_value=['192.0.2.10']), \
                patch.object(discovery_policy, 'fetch_config_from_edge', return_value=LATEST), \
                patch('sys.stderr', io.StringIO()):
            candidate = discovery_policy.discover(get_policy('qq'))
        self.assertEqual(candidate.version_code, 42775)

    def test_discovery_fails_when_no_source_returns_a_valid_release(self):
        invalid = {'android': dict(LATEST['android'], x64Link='https://example.org/qq.apk')}
        with patch.object(discovery_policy, 'fetch_config', side_effect=OSError('offline')), \
                patch.object(discovery_policy, 'mainland_edge_addresses', return_value=['192.0.2.10']), \
                patch.object(discovery_policy, 'fetch_config_from_edge', return_value=invalid), \
                patch('sys.stderr', io.StringIO()):
            with self.assertRaisesRegex(ValueError, 'No official QQ configuration source'):
                discovery_policy.discover(get_policy('qq'))

    def test_discovery_rejects_conflicting_downloads_for_the_same_release(self):
        conflict = qq_config('9.3.80', '9.3.80.42775_0123456789abcdef.apk')
        with patch.object(discovery_policy, 'fetch_config', return_value=LATEST), \
                patch.object(discovery_policy, 'mainland_edge_addresses', return_value=['192.0.2.10']), \
                patch.object(discovery_policy, 'fetch_config_from_edge', return_value=conflict), \
                patch('sys.stderr', io.StringIO()):
            with self.assertRaisesRegex(ValueError, 'disagree on the latest download'):
                discovery_policy.discover(get_policy('qq'))

    def test_mainland_edge_lookup_keeps_one_new_ipv4_per_subnet_and_skips_failures(self):
        first, second, third = discovery_policy.MAINLAND_CLIENT_SUBNETS
        answers = {
            first: [{'type': 5, 'data': 'cname.example.'},
                    {'type': 1, 'data': '192.0.2.10'}, {'type': 1, 'data': '192.0.2.11'}],
            third: [{'type': 1, 'data': '192.0.2.10'}, {'type': 1, 'data': '192.0.2.12'}],
        }
        queries = []

        def resolver(request, timeout):
            queries.append(request.full_url)
            query = parse_qs(urlsplit(request.full_url).query)
            self.assertEqual(query['name'], ['qq-web.cdn-go.cn'])
            self.assertEqual(timeout, discovery_policy.DOH_TIMEOUT_SECONDS)
            subnet = query['edns_client_subnet'][0]
            if subnet == second:
                raise OSError('resolver unreachable')
            return doh_response({'Answer': answers[subnet]})

        with patch.object(discovery_policy, 'urlopen', side_effect=resolver), \
                patch('sys.stderr', io.StringIO()) as log:
            addresses = discovery_policy.mainland_edge_addresses()
        self.assertEqual(addresses, ['192.0.2.10', '192.0.2.12'])
        self.assertEqual(len(queries), 3)
        self.assertTrue(all(query.startswith(discovery_policy.DOH_RESOLVER + '?') for query in queries))
        self.assertIn('resolver unreachable', log.getvalue())

    def test_mainland_edge_lookup_returns_completed_subnets_at_the_deadline(self):
        release = threading.Event()
        first = discovery_policy.MAINLAND_CLIENT_SUBNETS[0]

        def resolve(subnet):
            if subnet == first:
                return ['192.0.2.10']
            release.wait(5)
            return ['192.0.2.20']

        started = time.monotonic()
        try:
            with patch.object(discovery_policy, 'resolve_for_subnet', side_effect=resolve), \
                    patch.object(discovery_policy, 'EDGE_LOOKUP_DEADLINE_SECONDS', 0.2), \
                    patch('sys.stderr', io.StringIO()) as log:
                addresses = discovery_policy.mainland_edge_addresses()
        finally:
            release.set()
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(addresses, ['192.0.2.10'])
        self.assertIn('exceeded 0.2s for 2 subnet(s)', log.getvalue())

    def test_edge_connection_verifies_tls_for_the_official_host(self):
        connection = discovery_policy.EdgeConnection('qq-web.cdn-go.cn', '192.0.2.10', timeout=30)
        self.assertTrue(connection.tls_context.check_hostname)
        with patch.object(discovery_policy.socket, 'create_connection', return_value='raw') as create, \
                patch.object(connection.tls_context, 'wrap_socket', return_value='tls') as wrap:
            connection.connect()
        create.assert_called_once_with(('192.0.2.10', 443), 30)
        wrap.assert_called_once_with('raw', server_hostname='qq-web.cdn-go.cn')
        self.assertEqual(connection.sock, 'tls')

    def test_manifest_expected_values_are_parsed_from_both_configurations(self):
        from scripts.ci.apps.qq.runtime_probe import manifest_app_ids
        xml = '<manifest xmlns:android="http://schemas.android.com/apk/res/android"><application>'
        xml += '<meta-data android:name="AppSetting_params" android:value="10#token" />'
        xml += '<meta-data android:name="AppSetting_params_pad" android:value="20#token" />'
        xml += '</application></manifest>'
        self.assertEqual(manifest_app_ids(xml), (10, 20))
        with self.assertRaises(ValueError):
            manifest_app_ids(xml.replace('20#token', '10#token'))
        with self.assertRaises(ValueError):
            manifest_app_ids(xml.replace('</application>', '<meta-data android:name="AppSetting_params" android:value="10#token" /></application>'))

    def test_runtime_promotion_requires_request_evidence(self):
        from scripts.ci.runtime_task import runtime_result
        identity = {'packageName': 'com.tencent.mobileqq', 'versionName': '9.3.1',
                    'versionCode': 14378, 'abi': 'arm64-v8a',
                    'apkSha256': 'a' * 64,
                    'signerSha256': get_policy('qq').signer_sha256}
        report = dict(appId='qq', status='STATIC_VERIFIED_PENDING_RUNTIME', identity=identity,
                      requestIdentityExpectation=expectation(identity['apkSha256']))
        smoke = dict(status='RUNTIME_SMOKE_VERIFIED', appId='qq', packageName='com.tencent.mobileqq',
                     abi='arm64-v8a', baseline='PHONE_REQUEST_APP_ID', hookIds=['device_type'],
                     apkSha256=identity['apkSha256'], moduleApkSha256='b'*64)
        self.assertEqual(runtime_result(report, smoke, 'success')['status'], 'RUNTIME_REJECTED')
        smoke.update(acceptanceKind='REQUEST_APP_ID_CONTRAST', requestIdentityEvidence=proof(identity['apkSha256']))
        self.assertEqual(runtime_result(report, smoke, 'success')['status'], 'RUNTIME_VERIFIED')
        smoke['requestIdentityEvidence']['enabled']['frames'][0]['packetAppId'] = 10
        self.assertEqual(runtime_result(report, smoke, 'success')['status'], 'RUNTIME_REJECTED')
        smoke['status'] = 'ACCEPTANCE_UNAVAILABLE'
        result = runtime_result(report, smoke, 'failure')
        self.assertEqual(result['status'], 'STATIC_VERIFIED_PENDING_RUNTIME')
        self.assertEqual(result['runtimeConclusion'], 'acceptance-unavailable')
        smoke.update(status='RUNTIME_SMOKE_VERIFIED', acceptanceKind='APP_ID_CONFIGURATION')
        self.assertEqual(runtime_result(report, smoke, 'success')['status'], 'RUNTIME_REJECTED')


if __name__ == '__main__':
    unittest.main()
