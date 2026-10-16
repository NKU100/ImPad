"""Inspect native Android Emulator availability and host acceleration without booting an AVD."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import urllib.request
import xml.etree.ElementTree as ET
import zipfile

REPOSITORY = 'https://dl.google.com/android/repository/'


def select_emulator(xml, system, machine):
    host_os = {'Linux': 'linux', 'Darwin': 'macosx', 'Windows': 'windows'}[system]
    host_arch = 'aarch64' if machine.lower() in {'arm64', 'aarch64'} else 'x64'
    candidates = []
    for package in ET.fromstring(xml).findall('remotePackage'):
        if package.get('path') != 'emulator':
            continue
        channel = package.find('channelRef')
        if channel is not None and channel.get('ref') != 'channel-0':
            continue
        revision = package.find('revision')
        version = tuple(int(revision.findtext(k, '0')) for k in ('major', 'minor', 'micro'))
        for archive in package.findall('archives/archive'):
            if archive.findtext('host-os') == host_os and archive.findtext('host-arch') == host_arch:
                complete = archive.find('complete')
                candidates.append((version, dict(version='.'.join(map(str, version)),
                    url=complete.findtext('url'), sha1=complete.findtext('checksum'),
                    hostOs=host_os, hostArch=host_arch)))
    return max(candidates, key=lambda item: item[0])[1] if candidates else None


def acceleration_status(code, output):
    if code != 0 or re.search(r'not (?:installed|supported|available|usable)|not found|disabled', output, re.I):
        return 'UNAVAILABLE'
    if re.search(r'KVM|WHPX|AEHD|Hypervisor.Framework|HAXM|HVF', output, re.I) and re.search(r'usable|supported|working', output, re.I):
        return 'AVAILABLE'
    return 'UNKNOWN'


def api31_images(xml):
    prefix = 'system-images;android-31;google_apis;'
    return sorted({p.get('path')[len(prefix):] for p in ET.fromstring(xml).findall('remotePackage')
                   if p.get('path', '').startswith(prefix)})


def command(args, timeout=30):
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return dict(command=args, exitCode=result.returncode, output=result.stdout + result.stderr)
    except (OSError, subprocess.TimeoutExpired) as error:
        return dict(command=args, exitCode=-1, output=str(error))


def collect_host(system):
    commands = {
        'Linux': [['uname', '-a'], ['lscpu'], ['free', '-k'], ['df', '-h', '.'], ['ls', '-l', '/dev/kvm']],
        'Darwin': [['sw_vers'], ['sysctl', 'hw.ncpu', 'hw.memsize', 'kern.hv_support'], ['df', '-h', '.']],
        'Windows': [['powershell', '-NoProfile', '-Command',
            'Get-CimInstance Win32_Processor | Select-Object Name,NumberOfCores,NumberOfLogicalProcessors,VirtualizationFirmwareEnabled,SecondLevelAddressTranslationExtensions | ConvertTo-Json; '
            'Get-CimInstance Win32_ComputerSystem | Select-Object TotalPhysicalMemory,HypervisorPresent | ConvertTo-Json; '
            'Get-WindowsOptionalFeature -Online -FeatureName HypervisorPlatform | Select-Object FeatureName,State | ConvertTo-Json']]
    }
    result = dict(system=system, machine=platform.machine(), cpuCount=os.cpu_count(),
                  diskFreeBytes=shutil.disk_usage('.').free, commands=[command(c) for c in commands[system]])
    if system == 'Linux':
        path = Path('/dev/kvm')
        result['kvmExists'] = path.exists()
        result['kvmReadWrite'] = os.access(path, os.R_OK | os.W_OK)
        if path.exists() and not result['kvmReadWrite']:
            result['kvmPermissionSetup'] = command(['sudo', '-n', 'chmod', '666', str(path)])
            result['kvmReadWrite'] = os.access(path, os.R_OK | os.W_OK)
    return result


def download(url, path):
    with urllib.request.urlopen(url, timeout=60) as source, path.open('wb') as target:
        shutil.copyfileobj(source, target)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    root = args.output
    root.mkdir(parents=True, exist_ok=True)
    system = platform.system()
    report = dict(runner=os.environ.get('PROBE_RUNNER', 'local'), host=collect_host(system),
                  acceleration='UNKNOWN', avdBoot='NOT_TESTED')
    try:
        catalog = root / 'repository.xml'
        download(REPOSITORY + 'repository2-3.xml', catalog)
        images = root / 'system-images.xml'
        download(REPOSITORY + 'sys-img/google_apis/sys-img2-3.xml', images)
        report['officialApi31ImageAbis'] = api31_images(images.read_bytes())
        selected = select_emulator(catalog.read_bytes(), system, platform.machine())
        report['officialEmulator'] = selected
        if selected is None:
            report['acceleration'] = 'NO_NATIVE_EMULATOR'
        else:
            package = root / 'emulator.zip'
            download(REPOSITORY + selected['url'], package)
            digest = hashlib.sha1()
            with package.open('rb') as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b''):
                    digest.update(chunk)
            if digest.hexdigest() != selected['sha1']:
                raise RuntimeError('Official Emulator archive checksum mismatch')
            with zipfile.ZipFile(package) as archive:
                archive.extractall(root / 'sdk')
                if system != 'Windows':
                    for entry in archive.infolist():
                        mode = entry.external_attr >> 16
                        path = root / 'sdk' / entry.filename
                        if mode and path.is_file():
                            path.chmod(mode & 0o777)
            package.unlink()
            executable = root / 'sdk/emulator' / ('emulator.exe' if system == 'Windows' else 'emulator')
            report['versionProbe'] = command([str(executable.resolve()), '-version'])
            probe = command([str(executable.resolve()), '-accel-check'])
            report['accelerationProbe'] = probe
            report['acceleration'] = acceleration_status(probe['exitCode'], probe['output'])
            (root / 'accel-check.txt').write_text(probe['output'])
    except Exception as error:
        report['error'] = str(error)
    (root / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
    summary = ('| Runner | Host | Native Emulator | Acceleration | AVD boot |\n'
               '|---|---|---|---|---|\n'
               f"| {report['runner']} | {system} {platform.machine()} | "
               f"{report.get('officialEmulator', {}).get('version', 'none') if report.get('officialEmulator') else 'none'} | "
               f"{report['acceleration']} | NOT_TESTED |\n")
    if report.get('error'):
        summary += f"\nProbe error: {report['error']}\n"
    (root / 'summary.md').write_text(summary)
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as target:
            target.write(summary)
    print(json.dumps({k: v for k, v in report.items() if k != 'host'}))
    return 1 if report.get('error') else 0


if __name__ == '__main__':
    raise SystemExit(main())
