"""QQ no-account request identity acceptance, independent of candidate Hook locators."""
import re
from scripts.ci.apps.wecom.runtime_policy import has_app_ui

hook_installation_log = 'installed 1 QQ hooks'
launcher_component = 'com.tencent.mobileqq/.activity.SplashActivity'
login_navigation_labels = ('同意', '同意并继续', '注册/登录', '登录', 'Agree', 'Agree and continue')


def tablet_entry_visible(text):
    return False


def login_input_ready(activity, xml, package_name):
    return ('LoginActivity' in activity or 'RegisterGuideActivity' in activity) and has_app_ui(xml, package_name)


def is_login_navigation_label(text):
    return text.strip() in login_navigation_labels


def qr_page_ready(activity, xml, package_name, page_activity):
    return False


def hook_diagnostics(logs):
    records = [line for line in logs.splitlines() if 'ImPad' in line
               and ('(com.tencent.mobileqq:MSF)' in line or 'package=com.tencent.mobileqq ' in line)
               and ('status=' in line or hook_installation_log in line)]
    return {'moduleLoaded': 'OBSERVED' if records else 'NOT_OBSERVED',
            'compatibility': 'COMPATIBLE' if any('status=COMPATIBLE' in line for line in records) else 'NOT_OBSERVED',
            'hookInstallation': 'INSTALLED_1' if any(hook_installation_log in line for line in records) else 'NOT_OBSERVED',
            'resolvedHooks': [], 'records': list(dict.fromkeys(records))[-12:]}


def validate_request_evidence(proof, apk_sha256, module_sha256, expectation):
    from scripts.ci.apps.qq.packet_capture import CAPTURE_KIND, observer_source_digest
    from scripts.ci.apps.qq.request_protocol import CONTRACT, SECURITY_COMMANDS
    digest = lambda value: isinstance(value, str) and re.fullmatch(r'[a-f0-9]{64}', value) is not None
    if not isinstance(proof, dict) or not digest(apk_sha256) or not digest(module_sha256):
        return False
    if proof.get('contract') != CONTRACT:
        return False
    if (not isinstance(expectation, dict) or expectation.get('contract') != CONTRACT
            or expectation.get('apkSha256') != apk_sha256
            or proof.get('phoneAppId') != expectation.get('phoneAppId')
            or proof.get('tabletAppId') != expectation.get('tabletAppId')):
        return False
    if any(type(expectation.get(key)) is not int for key in ('phoneAppId', 'tabletAppId')):
        return False
    phone, tablet = proof.get('phoneAppId'), proof.get('tabletAppId')
    if type(phone) is not int or type(tablet) is not int or not 0 < phone <= 0x7fffffff or not 0 < tablet <= 0x7fffffff or phone == tablet:
        return False
    observer = observer_source_digest()
    uids = set()
    binaries = set()
    commands = []
    for key, enabled, expected in [('disabled', False, phone), ('enabled', True, tablet)]:
        value = proof.get(key)
        if not isinstance(value, dict) or value.get('contract') != CONTRACT:
            return False
        if (value.get('moduleEnabled') is not enabled or value.get('scopeVerified') is not True
                or value.get('captureHealthy') is not True or value.get('errors') != []
                or value.get('apkSha256') != apk_sha256 or value.get('moduleApkSha256') != module_sha256
                or value.get('observerSha256') != observer or value.get('processName') != 'com.tencent.mobileqq:MSF'):
            return False
        if (value.get('ndkVersion') != '29.0.14206865' or value.get('captureKind') != CAPTURE_KIND or value.get('ownershipVerified') is not True
                or type(value.get('kernelDrops')) is not int or value['kernelDrops'] != 0
                or type(value.get('partialStreams')) is not int or value['partialStreams'] != 0
                or type(value.get('appUid')) is not int or not 10000 <= value['appUid'] <= 0x7fffffff
                or not digest(value.get('collectorBinarySha256'))):
            return False
        owners = value.get('owners')
        if not isinstance(owners, list) or not 1 <= len(owners) <= 64:
            return False
        for owner in owners:
            if (not isinstance(owner, dict) or any(type(owner.get(k)) is not int or not 0 < owner[k] <= 0xffffffffffffffff for k in ('pid', 'uid', 'started', 'inode'))
                    or owner['uid'] != value['appUid'] or owner['pid'] > 0x7fffffff):
                return False
        uids.add(value['appUid']); binaries.add(value['collectorBinarySha256'])
        frames = value.get('frames')
        if not isinstance(frames, list) or not 1 <= len(frames) <= 2048:
            return False
        seen = set()
        for frame in frames:
            if (not isinstance(frame, dict) or not isinstance(frame.get('command'), str) or frame['command'] not in SECURITY_COMMANDS
                    or type(frame.get('packetAppId')) is not int or frame['packetAppId'] != expected
                    or type(frame.get('sequence')) is not int or not 0 <= frame['sequence'] <= 0xffffffff):
                return False
            seen.add(frame['command'])
        commands.append(seen)
    return len(uids) == len(binaries) == 1 and bool(commands[0] & commands[1])
