"""QQ no-account request identity acceptance, independent of candidate Hook locators."""
import re
import xml.etree.ElementTree as ET
from scripts.ci.apps.wecom.runtime_policy import has_app_ui
from scripts.ci.apps.qq.request_protocol import CONTRACT, SECURITY_COMMANDS

EXPECTATION_FIELDS = frozenset({'contract', 'apkSha256', 'phoneAppId', 'tabletAppId'})
PROOF_FIELDS = frozenset({'contract', 'apkSha256', 'phoneAppId', 'tabletAppId', 'disabled', 'enabled'})
CAPTURE_EVIDENCE_FIELDS = frozenset({
    'contract', 'captureKind', 'frames', 'captureHealthy', 'errors', 'kernelDrops',
    'partialStreams', 'ownershipVerified', 'owners', 'moduleEnabled', 'scopeVerified',
    'observerSha256', 'collectorBinarySha256', 'ndkVersion', 'apkSha256', 'appUid',
    'moduleApkSha256', 'processName',
})
OWNER_EVIDENCE_FIELDS = frozenset({'pid', 'uid', 'started', 'inode'})
FRAME_EVIDENCE_FIELDS = frozenset({'command', 'packetAppId', 'sequence', 'firstObservedMonotonicNs'})

hook_installation_log = 'installed 1 QQ hooks'
launcher_component = 'com.tencent.mobileqq/.activity.SplashActivity'
login_navigation_labels = ('同意', '同意并继续', '注册/登录', '登录', 'Agree', 'Agree and continue')


def tablet_entry_visible(text):
    return False


def login_input_ready(activity, xml, package_name):
    if not ('LoginActivity' in activity or 'RegisterGuideActivity' in activity):
        return False
    try:
        nodes = ET.fromstring(xml).iter('node')
    except ET.ParseError:
        return False
    for node in nodes:
        bounds = re.findall(r'\d+', node.get('bounds', ''))
        if (node.get('package') == package_name and node.get('visible') != 'false'
                and node.get('enabled') != 'false'
                and ('EditText' in node.get('class', '') or 'AutoCompleteTextView' in node.get('class', ''))
                and len(bounds) == 4 and int(bounds[2]) > int(bounds[0]) and int(bounds[3]) > int(bounds[1])):
            return True
    return False


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


def build_request_identity_proof(expectation, disabled, enabled):
    """Build the exact request proof from an APK-manifest expectation and captures."""
    if (not isinstance(expectation, dict) or set(expectation) != EXPECTATION_FIELDS
            or expectation.get('contract') != CONTRACT
            or not isinstance(expectation.get('apkSha256'), str)
            or re.fullmatch(r'[a-f0-9]{64}', expectation['apkSha256']) is None):
        raise ValueError('QQ request identity expectation has an invalid schema or APK identity')
    phone, tablet = expectation.get('phoneAppId'), expectation.get('tabletAppId')
    if (type(phone) is not int or type(tablet) is not int
            or not 0 < phone <= 0x7fffffff or not 0 < tablet <= 0x7fffffff
            or phone == tablet):
        raise ValueError('QQ request identity expectation has invalid phone/tablet AppIDs')
    for label, capture in (('disabled', disabled), ('enabled', enabled)):
        if (not isinstance(capture, dict) or set(capture) != CAPTURE_EVIDENCE_FIELDS
                or capture.get('contract') != CONTRACT
                or capture.get('apkSha256') != expectation['apkSha256']):
            raise ValueError(f'QQ {label} request capture has an invalid schema or APK identity')
    return {'contract': CONTRACT, 'apkSha256': expectation['apkSha256'],
            'phoneAppId': phone, 'tabletAppId': tablet,
            'disabled': disabled, 'enabled': enabled}


def validate_request_evidence(proof, apk_sha256, module_sha256, expectation):
    from scripts.ci.apps.qq.packet_capture import CAPTURE_KIND, observer_source_digest
    digest = lambda value: isinstance(value, str) and re.fullmatch(r'[a-f0-9]{64}', value) is not None
    if (not isinstance(proof, dict) or set(proof) != PROOF_FIELDS
            or not digest(apk_sha256) or not digest(module_sha256)):
        return False
    if proof.get('contract') != CONTRACT or proof.get('apkSha256') != apk_sha256:
        return False
    if (not isinstance(expectation, dict) or set(expectation) != EXPECTATION_FIELDS
            or expectation.get('contract') != CONTRACT
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
        if (not isinstance(value, dict) or set(value) != CAPTURE_EVIDENCE_FIELDS
                or value.get('contract') != CONTRACT):
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
            if (not isinstance(owner, dict) or set(owner) != OWNER_EVIDENCE_FIELDS
                    or any(type(owner.get(k)) is not int or not 0 < owner[k] <= 0xffffffffffffffff for k in ('pid', 'uid', 'started', 'inode'))
                    or owner['uid'] != value['appUid'] or owner['pid'] > 0x7fffffff):
                return False
        uids.add(value['appUid']); binaries.add(value['collectorBinarySha256'])
        frames = value.get('frames')
        if not isinstance(frames, list) or not 1 <= len(frames) <= 2048:
            return False
        seen = set()
        for frame in frames:
            if (not isinstance(frame, dict) or set(frame) != FRAME_EVIDENCE_FIELDS
                    or not isinstance(frame.get('command'), str) or frame['command'] not in SECURITY_COMMANDS
                    or type(frame.get('packetAppId')) is not int or frame['packetAppId'] != expected
                    or type(frame.get('sequence')) is not int or not 0 <= frame['sequence'] <= 0xffffffff
                    or type(frame.get('firstObservedMonotonicNs')) is not int
                    or frame['firstObservedMonotonicNs'] <= 0):
                return False
            seen.add(frame['command'])
        commands.append(seen)
    return len(uids) == len(binaries) == 1 and bool(commands[0] & commands[1])
