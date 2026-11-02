"""Trusted, profile-independent QQ request identity observation."""
import base64
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import tempfile
from pathlib import PurePosixPath
import xml.etree.ElementTree as ET
from scripts.ci.apps.qq.request_protocol import CONTRACT

ANDROID = '{http://schemas.android.com/apk/res/android}'


def manifest_app_ids(xml):
    values = {}
    for node in ET.fromstring(xml).iter('meta-data'):
        key = node.get(ANDROID + 'name')
        if key not in ('AppSetting_params', 'AppSetting_params_pad'):
            continue
        if key in values:
            raise ValueError('Duplicate QQ Manifest configuration')
        value = node.get(ANDROID + 'value', '').split('#')[0]
        if not re.fullmatch(r'[1-9][0-9]*', value):
            raise ValueError('Invalid QQ Manifest AppID')
        values[key] = int(value)
    phone, tablet = values['AppSetting_params'], values['AppSetting_params_pad']
    if phone == tablet or max(phone, tablet) > 0x7fffffff:
        raise ValueError('QQ phone/tablet AppIDs must be distinct valid integers')
    return phone, tablet


def manifest_expectation(apk, expected_sha256):
    from scripts.ci.fetch_latest_wechat_apk import file_sha256
    apk = Path(apk)
    if file_sha256(apk) != expected_sha256:
        raise ValueError('Manifest APK differs from the trusted candidate')
    sdk = Path(os.environ.get('ANDROID_HOME') or os.environ.get('ANDROID_SDK_ROOT', ''))
    xml = subprocess.check_output([str(sdk / 'cmdline-tools/latest/bin/apkanalyzer'),
                                   'manifest', 'print', str(apk)], timeout=60).decode()
    phone, tablet = manifest_app_ids(xml)
    if file_sha256(apk) != expected_sha256:
        raise ValueError('Manifest APK changed during extraction')
    return {'contract': CONTRACT, 'apkSha256': expected_sha256,
            'phoneAppId': phone, 'tabletAppId': tablet}


def manager_state_evidence(smoke):
    with tempfile.TemporaryDirectory() as directory:
        database = Path(directory) / 'modules_config.db'
        for suffix in ('', '-wal'):
            result = smoke.su('base64 /data/adb/lspd/config/modules_config.db' + suffix, check=False)
            if result.returncode == 0:
                Path(str(database) + suffix).write_bytes(base64.b64decode(b''.join(result.stdout.split()), validate=True))
            elif not suffix:
                raise RuntimeError('Cannot independently read LSPosed module state')
        connection = sqlite3.connect(database)
        try:
            enabled = connection.execute(
                'SELECT module_pkg_name, user_id, enabled FROM modules_state WHERE module_pkg_name=? AND user_id=0',
                (smoke.MODULE,),
            ).fetchall()
            scope = connection.execute(
                'SELECT module_pkg_name, app_pkg_name, user_id FROM scope WHERE module_pkg_name=? AND app_pkg_name=? AND user_id=0',
                (smoke.MODULE, smoke.APP_PACKAGE),
            ).fetchall()
        finally:
            connection.close()
    if len(enabled) != 1 or enabled[0][2] not in (0, 1) or not scope:
        raise RuntimeError('QQ module state or LSPosed scope is missing')
    return {
        'schemaVersion': 1,
        'module': smoke.MODULE,
        'packageName': smoke.APP_PACKAGE,
        'moduleStateRows': [
            {'modulePackage': row[0], 'userId': row[1], 'enabled': row[2]}
            for row in enabled
        ],
        'scopeRows': [
            {'modulePackage': row[0], 'appPackage': row[1], 'userId': row[2]}
            for row in scope
        ],
    }


def manager_state(smoke):
    raw = manager_state_evidence(smoke)
    row = raw['moduleStateRows'][0]
    return {'moduleEnabled': bool(row['enabled']), 'scopeVerified': bool(raw['scopeRows'])}


def installed_hash(smoke, package):
    rows = smoke.adb('shell', 'pm', 'path', package).stdout.decode().splitlines()
    base = [row.removeprefix('package:') for row in rows if row.endswith('/base.apk')]
    if len(base) != 1:
        raise ValueError('Installed base APK is ambiguous')
    return smoke.adb('shell', 'sha256sum', base[0]).stdout.decode().split()[0]


def installed_identity(smoke, package):
    rows = smoke.adb('shell', 'pm', 'path', package).stdout.decode().splitlines()
    if any(not row.startswith('package:') for row in rows):
        raise ValueError('Installed APK path output is malformed')
    paths = [row.removeprefix('package:') for row in rows]
    if any(not PurePosixPath(path).is_absolute() or '..' in PurePosixPath(path).parts
           or not path.endswith('.apk') for path in paths):
        raise ValueError('Installed APK path is malformed')
    base = [path for path in paths if path.endswith('/base.apk')]
    if len(base) != 1:
        raise ValueError('Installed base APK is ambiguous')

    def installed_digest(path):
        fields = smoke.adb('shell', 'sha256sum', path).stdout.decode().split()
        if len(fields) < 2 or fields[1] != path or not re.fullmatch(r'[a-fA-F0-9]{64}', fields[0]):
            raise ValueError('Installed APK digest output is malformed')
        return fields[0].lower()

    identity = {'packageName': package, 'baseApkPath': base[0], 'baseApkSha256': installed_digest(base[0])}
    splits = [path for path in paths if path != base[0]]
    if len(paths) > 1:
        if len(set(paths)) != len(paths):
            raise ValueError('Installed APK paths are duplicated')
        identity['splitApks'] = [{'path': path, 'sha256': installed_digest(path)} for path in splits]
    return identity
