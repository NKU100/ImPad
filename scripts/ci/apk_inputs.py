"""Keep original signed base/split APKs intact across candidate artifacts."""

import json
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import zipfile

from scripts.ci.fetch_latest_wechat_apk import MAX_APK_BYTES, file_sha256


def unpack_apk_inputs(download, directory, package_name):
    download, directory = Path(download), Path(directory)
    if not zipfile.is_zipfile(download):
        # The shared APK inspector rejects non-APK responses before publication.
        return [download]
    with zipfile.ZipFile(download) as archive:
        entries = archive.infolist()
        names = [item.filename for item in entries]
        if len(names) != len(set(names)):
            raise ValueError('APK archive has duplicate entries')
        if 'AndroidManifest.xml' in names:
            return [download]
        if len(entries) > 256 or sum(item.file_size for item in entries) > MAX_APK_BYTES:
            raise ValueError('XAPK expanded size or entry count exceeds the safety limit')
        for item in entries:
            path = PurePosixPath(item.filename)
            mode = item.external_attr >> 16
            if (path.is_absolute() or '..' in path.parts or '\\' in item.filename
                    or stat.S_ISLNK(mode) or item.flag_bits & 1):
                raise ValueError('XAPK contains an unsafe archive entry')
            if item.filename.lower().endswith('.obb'):
                raise ValueError('XAPK expansion files are not supported')
        if 'manifest.json' not in names or archive.getinfo('manifest.json').file_size > 1024 * 1024:
            raise ValueError('Download is neither an APK nor a supported XAPK')
        manifest = json.loads(archive.read('manifest.json'))
        if not isinstance(manifest, dict) or manifest.get('package_name', package_name) != package_name:
            raise ValueError('XAPK package differs from the requested application')
        apks = [item for item in entries if item.filename.lower().endswith('.apk')]
        bases = [item for item in apks
                 if PurePosixPath(item.filename).name in ('base.apk', package_name + '.apk')]
        if len(bases) != 1 or len(apks) > 128:
            raise ValueError('XAPK requires exactly one base APK and at most 127 splits')
        ordered = bases + sorted((item for item in apks if item != bases[0]), key=lambda item: item.filename)
        directory.mkdir(parents=True, exist_ok=True)
        result = []
        for index, item in enumerate(ordered):
            target = directory / f'{index}.apk'
            if target.is_symlink():
                raise ValueError('XAPK output must not be a symbolic link')
            with archive.open(item) as source, target.open('wb') as output:
                shutil.copyfileobj(source, output, length=1024 * 1024)
            if target.stat().st_size != item.file_size:
                raise ValueError('XAPK expanded APK size differs from the archive')
            result.append(target)
        return result


def resolve_apk_inputs(candidate, root):
    root = Path(root).resolve()
    items = candidate.get('apkFiles')
    if not isinstance(items, list) or not items:
        raise ValueError('Candidate has no exact base/split APK identities')
    result = []
    for index, item in enumerate(items):
        if (not isinstance(item, dict) or item.get('role') != ('base' if index == 0 else 'split')
                or not isinstance(item.get('path'), str) or not item['path']
                or not re.fullmatch(r'[a-fA-F0-9]{64}', str(item.get('sha256', '')))):
            raise ValueError('Candidate base/split APK identity is malformed')
        relative = Path(item['path'])
        if relative.is_absolute() or '..' in relative.parts:
            raise ValueError('Candidate APK path escapes its artifact')
        path = (root / relative).resolve()
        path.relative_to(root)
        if path in result:
            raise ValueError('Candidate repeats an APK input')
        if not path.is_file() or file_sha256(path) != item['sha256'].lower():
            raise ValueError('Candidate APK changed after verification')
        result.append(path)
    if items[0]['sha256'].lower() != candidate.get('identity', {}).get('apkSha256', '').lower():
        raise ValueError('Candidate base APK differs from its identity')
    return result


def verify_installed_apk_hashes(installed, apk_files):
    expected = [item['sha256'].lower() for item in apk_files]
    if not expected or installed.get('baseApkSha256') != expected[0]:
        raise ValueError('Installed base APK hash differs from the verified input')
    splits = installed.get('splitApks', [])
    if not isinstance(splits, list):
        raise ValueError('Installed split APK hashes are invalid')
    hashes, paths = [], []
    for item in splits:
        if (not isinstance(item, dict) or not isinstance(item.get('sha256'), str)
                or not re.fullmatch(r'[a-f0-9]{64}', item['sha256'])
                or not isinstance(item.get('path'), str) or not PurePosixPath(item['path']).is_absolute()
                or '..' in PurePosixPath(item['path']).parts):
            raise ValueError('Installed split APK identity is malformed')
        hashes.append(item['sha256'])
        paths.append(item['path'])
    if (len(paths) != len(set(paths)) or len(hashes) != len(set(hashes))
            or sorted(hashes) != sorted(expected[1:])):
        raise ValueError('Installed split APK hashes differ from the verified input')
