"""Give games a device view containing only hyprcontroller's virtual gamepads."""
import os
from pathlib import Path
import shutil

from evdev import InputDevice
from .bridge import PREFIX


def steam_running():
    for p in Path('/proc').glob('[0-9]*/comm'):
        try:
            if p.read_text().strip() == 'steam' and p.stat().st_uid == os.getuid():
                return True
        except OSError:
            pass
    return False


def build_command(command, status):
    if not command:
        raise ValueError('Supply a game command after -- (for example: game -- steam)')
    executable = shutil.which('bwrap')
    if not executable:
        raise RuntimeError('Bubblewrap (bwrap) is required for isolated controller routing')
    if status.get('input_mode') != 'bridge' or not status.get('virtual_controllers'):
        raise RuntimeError('Start hyprcontroller with run --bridge and connect a controller first')
    if Path(command[0]).name in ('steam', 'steam.sh') and steam_running():
        raise RuntimeError('Exit your games and Steam normally first, then launch Steam with this command')
    # A separate /dev also hides future raw HID/USB nodes after reconnects.
    args = [executable, '--bind', '/', '/', '--dev', '/dev', '--unshare-pid', '--proc', '/proc']
    for name in ('dri', 'snd', 'shm', 'ntsync', 'kfd', 'nvidia0', 'nvidiactl', 'nvidia-uvm', 'nvidia-modeset'):
        path = Path('/dev') / name
        if path.exists():
            args += ['--dev-bind', str(path), str(path)]
    args += ['--dir', '/dev/input']
    for bridge in status['virtual_controllers']:
        path = bridge['path']
        # Do not trust arbitrary paths received over the local status socket.
        if Path(path).parent != Path('/dev/input') or not Path(path).name.startswith('event'):
            raise RuntimeError('Invalid virtual controller path')
        device = InputDevice(path)
        try:
            if not device.name.startswith(PREFIX) or device.phys != 'hyprcontroller/bridge':
                raise RuntimeError('Virtual controller changed; retry after checking status')
        finally:
            device.close()
        for node in bridge['nodes']:
            if Path(node).parent != Path('/dev/input') or not Path(node).name.startswith(('event', 'js')):
                raise RuntimeError('Invalid virtual controller node')
            if Path(node).exists():
                args += ['--dev-bind', node, node]
    return args + ['--', *command]


def launch(command, status):
    args = build_command(command, status)
    os.execv(args[0], args)
