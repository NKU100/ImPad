import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

from scripts.ci.apk_inputs import unpack_apk_inputs, resolve_apk_inputs, verify_installed_apk_hashes


class ApkInputsTest(unittest.TestCase):
    def test_installed_hashes_require_exact_split_set(self):
        inputs = [{'sha256': 'a' * 64}, {'sha256': 'b' * 64}]
        installed = {'baseApkSha256': 'a' * 64,
                     'splitApks': [{'path': '/data/app/split.apk', 'sha256': 'b' * 64}]}
        verify_installed_apk_hashes(installed, inputs)
        for splits in ([], installed['splitApks'] * 2,
                       [{'path': '/data/app/split.apk', 'sha256': 'c' * 64}]):
            with self.subTest(splits=splits), self.assertRaises(ValueError):
                verify_installed_apk_hashes(dict(installed, splitApks=splits), inputs)

    def test_xapk_keeps_base_and_every_split_in_order(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / 'download.apk'
            with zipfile.ZipFile(archive, 'w') as output:
                output.writestr('config.en.apk', b'language')
                output.writestr('com.tencent.mm.apk', b'base')
                output.writestr('config.arm64_v8a.apk', b'native')
                output.writestr('manifest.json', json.dumps({'package_name': 'com.tencent.mm'}))
            files = unpack_apk_inputs(archive, root / 'unpacked', 'com.tencent.mm')
            self.assertEqual([b'base', b'native', b'language'], [item.read_bytes() for item in files])

    def test_archive_rejects_traversal_duplicate_base_and_obb(self):
        for extra in ('../escape.apk', '/escape.apk', 'nested\\escape.apk', 'base.apk', 'main.obb'):
            with self.subTest(extra=extra), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                archive = root / 'input.xapk'
                with zipfile.ZipFile(archive, 'w') as output:
                    output.writestr('com.tencent.mm.apk', b'base')
                    output.writestr(extra, b'extra')
                    output.writestr('manifest.json', '{}')
                with self.assertRaises(ValueError):
                    unpack_apk_inputs(archive, root / 'out', 'com.tencent.mm')

    def test_single_apk_is_not_unpacked_or_resigned(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            apk = root / 'input'
            with zipfile.ZipFile(apk, 'w') as output:
                output.writestr('AndroidManifest.xml', b'binary manifest')
            self.assertEqual([apk], unpack_apk_inputs(apk, root / 'out', 'com.tencent.mm'))

    def test_resolver_checks_every_split_and_rejects_modified_or_escaping_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            items = []
            for index, content in enumerate((b'base', b'split')):
                path = root / f'{index}.apk'
                path.write_bytes(content)
                items.append({'path': path.name, 'role': 'base' if index == 0 else 'split',
                              'sha256': hashlib.sha256(content).hexdigest()})
            candidate = {'identity': {'apkSha256': items[0]['sha256']}, 'apkFiles': items}
            self.assertEqual([(root / '0.apk').resolve(), (root / '1.apk').resolve()],
                             resolve_apk_inputs(candidate, root))
            (root / '1.apk').write_bytes(b'tampered')
            with self.assertRaisesRegex(ValueError, 'changed'):
                resolve_apk_inputs(candidate, root)
            items[1]['path'] = '../escape.apk'
            with self.assertRaises(ValueError):
                resolve_apk_inputs(candidate, root)
