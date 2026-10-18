import contextlib
import io
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'scripts'))
from backup import digest, inventory, verify
from install import install, rollback, update


class InstallationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.home = self.base / 'home'
        self.home.mkdir()
        self.backup = self.base / 'backup'
        self.backup.mkdir()
        original = self.home / '.config/hyprcontroller/config.json'
        original.parent.mkdir(parents=True)
        original.write_text('{"legacy": true}')
        self.original = original
        entries = inventory([original])
        archive = self.backup / 'desktop.tar.gz'
        with tarfile.open(archive, 'w:gz') as tf:
            tf.add(original, arcname=str(original).lstrip('/'))
        manifest = {'entries': entries, 'verified': True, 'archive_sha256': digest(archive)}
        (self.backup / 'manifest.json').write_text(json.dumps(manifest))

    def tearDown(self):
        self.tmp.cleanup()

    def silent(self, fn, *args, **kwargs):
        with contextlib.redirect_stdout(io.StringIO()):
            return fn(*args, **kwargs)

    def test_install_and_rollback_preserve_original_configuration(self):
        self.silent(install, self.home, self.backup)
        manifest = self.home / '.local/state/hyprcontroller/install.json'
        self.assertTrue(manifest.exists())
        self.silent(rollback, self.home)
        self.assertEqual(self.original.read_text(), '{"legacy": true}')
        self.assertFalse((self.home / '.local/bin/hyprcontroller').exists())
        self.assertFalse((self.home / '.config/hyprcontroller/overlay.json').exists())
        self.assertTrue((self.backup / 'desktop.tar.gz').exists())

    def test_later_edits_block_rollback_without_removing_anything(self):
        self.silent(install, self.home, self.backup)
        config = self.home / '.config/hyprcontroller/overlay.json'
        config.write_text('{"monitor": "DP-3"}')
        with self.assertRaises(RuntimeError):
            self.silent(rollback, self.home)
        self.assertTrue((self.home / '.local/bin/hyprcontroller').exists())
        result = self.silent(rollback, self.home, dry_run=True)
        self.assertIn(str(config), result['modified'])
        self.silent(rollback, self.home, keep_modified=True)
        self.assertEqual(config.read_text(), '{"monitor": "DP-3"}')
        self.assertFalse((self.home / '.local/bin/hyprcontroller').exists())

    def test_archive_verification_preserves_links_and_file_modes(self):
        root = self.base / 'linked'
        root.mkdir(); target = root / 'private'
        target.write_text('private'); target.chmod(0o600)
        (root / 'alias').symlink_to(target)
        entries = inventory([root])
        archive = self.base / 'links.tar.gz'
        with tarfile.open(archive, 'w:gz') as tf:
            for name in sorted(entries):
                tf.inodes.clear(); tf.add('/' + name, arcname=name, recursive=False)
        verify(archive, {'entries': entries})
        with tarfile.open(archive) as tf:
            member = tf.getmember(str(target).lstrip('/'))
            self.assertEqual(member.mode, 0o600)

    def test_corrupt_backup_is_refused(self):
        archive = self.backup / 'desktop.tar.gz'
        archive.write_bytes(archive.read_bytes() + b'changed')
        with self.assertRaises(RuntimeError):
            self.silent(install, self.home, self.backup)
        self.assertFalse((self.home / '.local/bin/hyprcontroller').exists())

    def test_update_preserves_configuration_and_has_verified_backup(self):
        self.silent(install, self.home, self.backup)
        config = self.home / '.config/hyprcontroller/overlay.json'
        before = config.read_bytes()
        self.silent(update, self.home)
        manifest = json.loads((self.home / '.local/state/hyprcontroller/install.json').read_text())
        backup = Path(manifest['updates'][-1]['backup'])
        saved = json.loads((backup / 'manifest.json').read_text())
        self.assertTrue(saved['verified'])
        verify(backup / 'installed.tar.gz', saved)
        self.assertEqual(config.read_bytes(), before)
        self.silent(rollback, self.home)
        self.assertTrue((backup / 'installed.tar.gz').exists())

    def test_update_refuses_later_edits_without_replacing_them(self):
        self.silent(install, self.home, self.backup)
        path = self.home / '.local/share/hyprcontroller-overlay/hyprcontroller/backend.py'
        path.write_text('user edit\n')
        with self.assertRaisesRegex(RuntimeError, 'Later edit'):
            self.silent(update, self.home)
        self.assertEqual(path.read_text(), 'user edit\n')

    def legacy_install_without_docs(self):
        self.silent(install, self.home, self.backup)
        app = self.home / '.local/share/hyprcontroller-overlay'
        manifest_path = self.home / '.local/state/hyprcontroller/install.json'
        manifest = json.loads(manifest_path.read_text())
        omitted = {str(app/name) for name in ('LICENSE', 'docs/DESIGN.md', 'docs/VALIDATION.md', 'docs/assets/switcher.png')}
        for name in omitted:
            Path(name).unlink()
        for directory in (app/'docs/assets', app/'docs'):
            directory.rmdir()
        manifest['files'] = [r for r in manifest['files'] if r['path'] not in omitted]
        manifest['created_directories'] = [p for p in manifest['created_directories'] if not Path(p).is_relative_to(app/'docs')]
        manifest_path.write_text(json.dumps(manifest))
        return app, manifest_path

    def test_install_includes_referenced_docs_and_removes_them_on_rollback(self):
        self.silent(install, self.home, self.backup)
        app = self.home / '.local/share/hyprcontroller-overlay'
        manifest = json.loads((self.home/'.local/state/hyprcontroller/install.json').read_text())
        owned = {r['path'] for r in manifest['files']}
        root = Path(__file__).resolve().parent.parent
        for name in ('LICENSE', 'docs/DESIGN.md', 'docs/VALIDATION.md', 'docs/assets/switcher.png'):
            self.assertEqual((app/name).read_bytes(), (root/name).read_bytes())
            self.assertIn(str(app/name), owned)
        self.silent(rollback, self.home)
        self.assertFalse(app.exists())

    def test_update_adds_missing_docs_with_rollback_and_later_edit_protection(self):
        app, manifest_path = self.legacy_install_without_docs()
        self.silent(update, self.home)
        manifest = json.loads(manifest_path.read_text())
        self.assertIn(str(app/'docs/assets'), manifest['created_directories'])
        self.assertTrue((app/'LICENSE').exists())
        screenshot = app/'docs/assets/switcher.png'
        original = screenshot.read_bytes()
        screenshot.write_text('User artwork')
        with self.assertRaisesRegex(RuntimeError, 'Later edits'):
            self.silent(rollback, self.home)
        self.assertEqual(screenshot.read_text(), 'User artwork')
        self.assertTrue((app/'LICENSE').exists())
        screenshot.write_bytes(original)
        self.silent(rollback, self.home)
        self.assertFalse(app.exists())

    def test_failed_update_removes_new_docs_and_restores_manifest(self):
        app, manifest_path = self.legacy_install_without_docs()
        before = manifest_path.read_bytes()
        replace = Path.replace

        def fail_license(path, target):
            if path.name == 'LICENSE.hyprcontroller-update':
                raise OSError('simulated write failure')
            return replace(path, target)

        with patch.object(Path, 'replace', fail_license):
            with self.assertRaisesRegex(OSError, 'simulated write failure'):
                self.silent(update, self.home)
        self.assertEqual(manifest_path.read_bytes(), before)
        self.assertFalse((app/'docs').exists())
        self.assertFalse((app/'LICENSE').exists())
        self.assertEqual(list(app.rglob('*.hyprcontroller-update')), [])

    def test_update_does_not_overwrite_unowned_documentation(self):
        app, manifest_path = self.legacy_install_without_docs()
        screenshot = app/'docs/assets/switcher.png'
        screenshot.parent.mkdir(parents=True)
        screenshot.write_text('User artwork')
        before = manifest_path.read_bytes()
        with self.assertRaisesRegex(RuntimeError, 'Unowned file'):
            self.silent(update, self.home)
        self.assertEqual(screenshot.read_text(), 'User artwork')
        self.assertEqual(manifest_path.read_bytes(), before)
