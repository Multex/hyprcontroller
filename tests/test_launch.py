import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch
from hyprcontroller.cli import request

ROOT = Path(__file__).resolve().parent.parent


@unittest.skipUnless(shutil.which('qs') and os.environ.get('HYPRLAND_INSTANCE_SIGNATURE') and os.environ.get('WAYLAND_DISPLAY'),
                     'Needs Quickshell in a Hyprland session')
class LaunchFolderTests(unittest.TestCase):
    def assert_backend_runs_from(self, base, decoy):
        folder = base / 'launch'
        marker = base / 'decoy-imported'
        (folder / decoy).parent.mkdir(parents=True)
        (folder / decoy).write_text(f'open({str(marker)!r}, "w").close()\nraise ImportError("decoy")\n')
        # PanelWindow needs real Wayland, so keep its socket while the app's own state stays in base.
        display = Path(os.environ.get('XDG_RUNTIME_DIR', f'/run/user/{os.getuid()}')) / os.environ['WAYLAND_DISPLAY']
        isolated = {'XDG_RUNTIME_DIR': str(base), 'XDG_CONFIG_HOME': str(base), 'XDG_CACHE_HOME': str(base),
                    'WAYLAND_DISPLAY': str(display)}
        with patch.dict(os.environ, isolated):
            app = subprocess.Popen([str(ROOT / 'bin/hyprcontroller'), 'run', '--preview'], cwd=folder,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                deadline = time.monotonic() + 15
                while True:
                    try:
                        request({'command': 'status'}, True)
                        break
                    except OSError:
                        self.assertIsNone(app.poll(), 'App exited before its backend answered')
                        self.assertLess(time.monotonic(), deadline, 'Backend did not answer')
                        time.sleep(.1)
                self.assertFalse(marker.exists())
                request({'command': 'stop'}, True)
                app.wait(timeout=5)
            finally:
                if app.poll() is None:
                    app.kill()
                    app.wait()

    def test_stray_package_in_launch_folder_is_ignored(self):
        with tempfile.TemporaryDirectory() as base:
            self.assert_backend_runs_from(Path(base), 'hyprcontroller/__init__.py')

    def test_stray_module_in_launch_folder_is_ignored(self):
        with tempfile.TemporaryDirectory() as base:
            self.assert_backend_runs_from(Path(base), 'evdev.py')
