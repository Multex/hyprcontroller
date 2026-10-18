import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'scripts'))
import backup


class BackupPortabilityTests(unittest.TestCase):
    def test_configuration_backup_survives_missing_inventory_tools(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            config = home / 'settings.json'
            config.write_text('{"monitor": "DP-3"}')
            config.chmod(0o600)
            with patch.multiple(backup, HOME=home, STATE=home/'state', SOURCES=[config]), \
                    patch('backup.shutil.which', return_value=None), \
                    patch('backup.subprocess.run', side_effect=FileNotFoundError('inventory tool missing')), \
                    contextlib.redirect_stdout(io.StringIO()) as output:
                backup.create()
            folder = Path(json.loads(output.getvalue())['backup'])
            manifest = json.loads((folder/'manifest.json').read_text())
            self.assertTrue(manifest['verified'])
            self.assertEqual(manifest['archive_sha256'], backup.digest(folder/'desktop.tar.gz'))
            backup.verify(folder/'desktop.tar.gz', manifest)
            self.assertIn('no supported package manager', (folder/'packages.txt').read_text())
            self.assertIn('unavailable', (folder/'user-services.txt').read_text())
            self.assertEqual(folder.stat().st_mode & 0o777, 0o700)
            self.assertEqual((folder/'desktop.tar.gz').stat().st_mode & 0o777, 0o600)

    def test_dpkg_inventory_is_used_when_pacman_is_absent(self):
        with patch('backup.shutil.which', side_effect=lambda name: '/usr/bin/dpkg-query' if name == 'dpkg-query' else None), \
                patch('backup.subprocess.run', return_value=subprocess.CompletedProcess([], 0, 'python3 3.12\n', '')) as run:
            text = backup.package_inventory()
        self.assertIn('dpkg-query -W', text)
        self.assertIn('python3 3.12', text)
        self.assertEqual(run.call_args.args[0], ['dpkg-query', '-W'])

    def test_arch_inventory_keeps_using_pacman(self):
        with patch('backup.shutil.which', return_value='/usr/bin/pacman'), \
                patch('backup.subprocess.run', return_value=subprocess.CompletedProcess([], 0, 'python 3.14\n', '')) as run:
            self.assertIn('python 3.14', backup.package_inventory())
        self.assertEqual(run.call_args.args[0], ['pacman', '-Q'])

    def test_inventory_timeout_and_failed_command_are_recorded(self):
        with patch('backup.subprocess.run', side_effect=subprocess.TimeoutExpired(['systemctl'], 10)):
            self.assertIn('unavailable', backup.command_output(['systemctl']))
        with patch('backup.subprocess.run', return_value=subprocess.CompletedProcess([], 1, '', 'No user bus')):
            self.assertIn('No user bus', backup.command_output(['systemctl']))
