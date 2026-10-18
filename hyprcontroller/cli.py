import argparse
import json
import os
from pathlib import Path
import shutil
import socket
import sys

from .backend import socket_path
from .config import Config


def request(command, preview=False):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.settimeout(4)
        conn.connect(str(socket_path(preview)))
        conn.sendall((json.dumps(command) + '\n').encode())
        result = b''
        while b'\n' not in result:
            chunk = conn.recv(8192)
            if not chunk:
                raise RuntimeError('hyprcontroller closed the connection')
            result += chunk
            if len(result) > 1024 * 1024:
                raise RuntimeError('Unexpected response size')
    return json.loads(result.split(b'\n', 1)[0])


def main():
    parser = argparse.ArgumentParser(description='Controller navigation for Hyprland')
    parser.add_argument('command', choices=['run', 'status', 'toggle', 'resume', 'stop', 'game'])
    parser.add_argument('--bridge', action='store_true', help='Forward controllers through isolated virtual gamepads')
    parser.add_argument('--config', type=Path)
    parser.add_argument('--preview', action='store_true', help='Use sample windows without touching input devices or Hyprland')
    args, child = parser.parse_known_args()
    if args.command == 'game':
        from .game import launch
        try:
            launch(child[1:] if child[:1] == ['--'] else child, request({'command': 'status'}))
        except (OSError, RuntimeError, ValueError) as exc:
            print(str(exc), file=sys.stderr)
            return 1
    if child:
        parser.error('Unexpected arguments: ' + ' '.join(child))
    if args.bridge and (args.command != 'run' or args.preview):
        parser.error('--bridge applies only to a live run')
    if args.command == 'run':
        try:
            Config.load(args.config)
            existing = request({'command': 'status'}, args.preview)
        except (OSError, RuntimeError):
            existing = None
        except ValueError as exc:
            parser.error(str(exc))
        if existing is not None:
            print('hyprcontroller is already running', file=sys.stderr)
            return 1
        if not args.preview and (not os.environ.get('HYPRLAND_INSTANCE_SIGNATURE') or not os.environ.get('WAYLAND_DISPLAY')):
            print('Run this command from your Hyprland session', file=sys.stderr)
            return 1
        executable = shutil.which('qs')
        if not executable:
            print('Quickshell (qs) is not installed', file=sys.stderr)
            return 1
        root = Path(__file__).resolve().parent.parent
        env = os.environ.copy()
        env['PYTHONPATH'] = str(root) + (os.pathsep + env['PYTHONPATH'] if env.get('PYTHONPATH') else '')
        env['HYPRCONTROLLER_PYTHON'] = sys.executable
        env['HYPRCONTROLLER_PREVIEW'] = '1' if args.preview else '0'
        env['HYPRCONTROLLER_BRIDGE'] = '1' if args.bridge else '0'
        if args.config:
            env['HYPRCONTROLLER_CONFIG'] = str(args.config.resolve())
        else:
            env.pop('HYPRCONTROLLER_CONFIG', None)
        os.execvpe(executable, [executable, '--no-duplicate', '-p', str(root / 'ui/shell.qml')], env)
    try:
        result = request({'command': args.command}, args.preview)
    except (OSError, RuntimeError) as exc:
        if args.command == 'status':
            print('hyprcontroller is not running')
        else:
            print(f'Cannot reach hyprcontroller: {exc}. Start it with hyprcontroller run.', file=sys.stderr)
        return 1
    if args.command == 'status':
        print(json.dumps(result, indent=2, ensure_ascii=False))
    elif result.get('error'):
        print(result['error'], file=sys.stderr)
        return 1
    else:
        print('Stopped' if args.command == 'stop' else f"Mode: {result['mode']}")
    return 0
