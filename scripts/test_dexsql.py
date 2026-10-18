import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.ci import codex_sandbox


class DexsqlTest(unittest.TestCase):
    def test_installer_verifies_downloads_and_keeps_licenses(self):
        from scripts.ci import install_dexsql
        payloads = {'dexsql-linux-arm64': b'#!/bin/sh\necho dexsql\n',
                    'LICENSE': b'binary license', 'LICENSE-AUXILIARY': b'auxiliary license'}
        hashes = {name: hashlib.sha256(data).hexdigest() for name, data in payloads.items()}

        def download(url, destination):
            destination.write_bytes(payloads[url.rsplit('/', 1)[1]])

        with tempfile.TemporaryDirectory() as temporary, \
                patch.object(install_dexsql, 'HASHES', hashes), \
                patch.object(install_dexsql, 'download', download):
            target = Path(temporary) / 'dexsql'
            binary = install_dexsql.install(target, 'Linux', 'aarch64')
            self.assertEqual(payloads['dexsql-linux-arm64'], binary.read_bytes())
            self.assertTrue(os.access(binary, os.X_OK))
            self.assertEqual(b'binary license', (target / 'LICENSE').read_bytes())
            self.assertEqual(b'auxiliary license', (target / 'LICENSE-AUXILIARY').read_bytes())

    def test_corrupt_download_does_not_replace_existing_tool(self):
        from scripts.ci import install_dexsql

        def download(url, destination):
            destination.write_bytes(b'corrupt download')

        with tempfile.TemporaryDirectory() as temporary, \
                patch.object(install_dexsql, 'download', download):
            target = Path(temporary) / 'dexsql'; (target / 'bin').mkdir(parents=True)
            binary = target / 'bin/dexsql'; binary.write_bytes(b'existing verified tool')
            with self.assertRaisesRegex(ValueError, 'SHA-256'):
                install_dexsql.install(target, 'Linux', 'aarch64')
            self.assertEqual(b'existing verified tool', binary.read_bytes())

    def test_sandbox_mounts_dexsql_directory_read_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            task = root / 'task'; (task / 'checkout').mkdir(parents=True)
            (task / 'state.json').write_text(json.dumps({}))
            tool = root / 'dexsql/bin/dexsql'; tool.parent.mkdir(parents=True)
            tool.write_text('#!/bin/sh\necho dexsql\n'); tool.chmod(0o755)
            env = {'HOME': str(root / 'home'), 'CODEX_HOME': str(root / 'codex-home'),
                   'PATH': str(tool.parent)}
            with patch.object(codex_sandbox.sys, 'platform', 'linux'):
                command = codex_sandbox.model_command(task, env, ['codex', 'app-server'])
            mounts = [command[i:i+3] for i, value in enumerate(command) if value == '--ro-bind']
            path = str((root / 'dexsql').resolve())
            self.assertIn(['--ro-bind', path, path], mounts)


if __name__ == '__main__':
    unittest.main()
