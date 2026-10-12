"""WeCom-specific login UI assertions used by the shared runtime smoke runner."""
import re
import xml.etree.ElementTree as ET


hook_installation_log = "installed 3 WeCom hooks"
login_input_activity = "LoginWxAuthActivity"
launcher_component = "com.tencent.wework/.launch.LaunchSplashActivity"
login_navigation_labels = (
    "agree", "accept", "登录", "同意", "接受",
)


def nodes(xml):
    try:
        return list(ET.fromstring(xml).iter("node"))
    except ET.ParseError:
        return []


def has_app_ui(xml, package_name):
    return any(node.get("package") == package_name for node in nodes(xml))


def tablet_entry_visible(text):
    return bool(re.search(
        r"login\s+on\s+pad\s+and\s+mobile\s+phone\s+at\s+the\s+same\s+time|平板.*手机.*同时登录",
        text,
        re.IGNORECASE,
    ))


def login_input_ready(activity, xml, package_name):
    ui_nodes = nodes(xml)
    if login_input_activity not in activity or not any(node.get("package") == package_name for node in ui_nodes):
        return False
    return not any(node.get("text", "").strip().lower() == "privacy policy" for node in ui_nodes)


def is_login_navigation_label(text):
    return text.strip().lower() in login_navigation_labels


def qr_page_ready(activity, xml, package_name, page_activity):
    ui_nodes = nodes(xml)
    if page_activity not in activity or not any(node.get("package") == package_name for node in ui_nodes):
        return False
    return any(re.search(r"QR\s*code|二维码", node.get("text", ""), re.IGNORECASE) for node in ui_nodes)


def hook_diagnostics(logs):
    records = [line for line in logs.splitlines()
               if "ImPad" in line and "process skipped:" not in line
               and any(marker in line for marker in ("status=", "waiting for Tinker", hook_installation_log))]
    compatible = any("status=COMPATIBLE" in line for line in records)
    installed = any(hook_installation_log in line for line in records)
    return {
        "moduleLoaded": "OBSERVED" if records else "NOT_OBSERVED",
        "compatibility": "COMPATIBLE" if compatible else "NOT_OBSERVED",
        "hookInstallation": "INSTALLED_3" if installed else "NOT_OBSERVED",
        "resolvedHooks": [],
        "records": list(dict.fromkeys(records))[-12:],
    }
