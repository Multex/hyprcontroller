"""Operate the existing shell keyboard without changing its configuration."""
import subprocess


class KeyboardError(RuntimeError):
    pass


class Keyboard:
    def __init__(self, config):
        self.config = config
        self.touched = False

    def call(self, action):
        try:
            result = subprocess.run(['qs', 'ipc', '-c', self.config.keyboard_config,
                                     'call', 'osk', action], capture_output=True, text=True, timeout=1.5)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise KeyboardError(f'Cannot reach the existing keyboard: {exc}') from exc
        if result.returncode:
            raise KeyboardError('Cannot reach the existing keyboard: ' +
                                (result.stderr.strip() or result.stdout.strip() or 'shell unavailable'))

    def open(self):
        self.call('open')
        self.touched = True

    def toggle(self):
        self.call('toggle')
        self.touched = True

    def close(self):
        if self.touched:
            self.call('close')
            self.touched = False
