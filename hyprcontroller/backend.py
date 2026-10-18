from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import selectors
import signal
import socket
import sys
import time

from evdev import InputDevice, UInputError, ecodes as e
from .config import Config
from .desktop import Desktop, DesktopError, event_socket, configure_overlay
from .input import ModeController, PadState
from .bridge import Bridge, PREFIX, identity
from .pointer import DesktopPointer
from .keyboard import Keyboard, KeyboardError


def runtime_dir():
    path = Path(os.environ.get('XDG_RUNTIME_DIR', f'/run/user/{os.getuid()}')) / 'hyprcontroller'
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.stat().st_uid != os.getuid() or path.is_symlink():
        raise RuntimeError('Unsafe runtime directory')
    path.chmod(0o700)
    return path


def socket_path(preview=False):
    return runtime_dir() / ('preview.sock' if preview else 'control.sock')


def compatible_gamepad(capabilities):
    keys = capabilities.get(e.EV_KEY, [])
    axes = {code for code, _ in capabilities.get(e.EV_ABS, [])}
    return e.BTN_GAMEPAD in keys and {e.ABS_X, e.ABS_Y} <= axes


def device_stamp(path):
    try:
        info = Path(path).stat()
    except OSError:
        return None
    return info.st_dev, info.st_ino, info.st_rdev, info.st_ctime_ns


class Pad:
    def __init__(self, device):
        self.device = device
        self.name = device.name
        self.bridge = None
        self.state = PadState(buttons=set(device.active_keys()))
        for code, info in device.capabilities().get(e.EV_ABS, []):
            self.state.axes[code] = info.value
            self.state.ranges[code] = info

    def sync(self):
        self.state.buttons = set(self.device.active_keys())
        for code in self.state.ranges:
            info = self.device.absinfo(code)
            self.state.axes[code] = info.value


class Service:
    def __init__(self, config, preview=False):
        self.config = config
        self.preview = preview
        self.machine = ModeController(config)
        self.desktop = Desktop()
        self.selector = selectors.DefaultSelector()
        self.pads = {}
        self.grabbed = None
        self.running = True
        self.selected = 0
        self.columns = 2
        self.error = ''
        self.error_sequence = 0
        self.loading = False
        self.selected_on_close = None
        self.ui_ready = False
        self.last_heartbeat = time.monotonic()
        self.visible_deadline = None
        self.socket = None
        self.lock = None
        self.events = None
        self.overlay_configured = False
        self.event_buffer = b''
        self.stdin_buffer = b''
        self.clients = {}
        self.refresh_at = None
        self.scan_at = 0
        self.event_retry_at = 0
        self.path = socket_path(preview)
        self.owns_socket = False
        self.scan_errors = []
        self.ignored_devices = {}
        self.bridges = []
        self.pointer = None
        self.keyboard = Keyboard(config)
        self.preview_data = {
            'monitor': '', 'workspace': {'id': 2, 'name': '2'},
            'workspaces': [{'id': n, 'name': str(n)} for n in range(1, 9)],
            'windows': [
                {'address': '0x1', 'title': 'Hollow Knight', 'app': 'Steam', 'active': True},
                {'address': '0x2', 'title': 'Music for the evening', 'app': 'Firefox', 'active': False},
                {'address': '0x3', 'title': 'Downloads', 'app': 'Files', 'active': False},
                {'address': '0x4', 'title': 'hyprcontroller', 'app': 'Terminal', 'active': False},
            ]}

    def data(self):
        return self.preview_data if self.preview else self.desktop.data

    def state(self):
        owner = self.pads.get(self.machine.owner)
        return {'type': 'state', 'pid': os.getpid(), 'mode': self.machine.mode,
                'controller': 'Preview controller' if self.preview else (owner.name if owner else ''),
                'controllers': [{'path': path, 'name': p.name} for path, p in self.pads.items()],
                'selected': self.selected, 'error': self.error, 'error_sequence': self.error_sequence,
                'loading': self.loading,
                'view': self.machine.view,
                'view_pending': self.machine.pending_view,
                'pointer_path': self.pointer.path if self.pointer else None,
                'keyboard_config': self.config.keyboard_config,
                'keyboard_used': self.keyboard.touched,
                'activation_hint': self.config.activation_hint,
                'input_mode': self.config.input_mode,
                'virtual_controllers': [{'path': b.path, 'nodes': b.nodes,
                                         'name': PREFIX + b.key[0], 'connected': b.source is not None}
                                        for b in self.bridges],
                'access_errors': self.scan_errors, **self.data()}

    def emit(self):
        try:
            print(json.dumps(self.state(), ensure_ascii=False), flush=True)
        except BrokenPipeError:
            self.running = False

    def fail(self, message):
        self.error = str(message)
        self.error_sequence += 1
        print(self.error, file=sys.stderr, flush=True)
        self.emit()

    def release(self):
        if self.pointer is not None:
            try:
                self.pointer.close()
            except OSError as exc:
                self.fail(f'Could not clean up desktop pointer: {exc}')
            finally:
                self.pointer = None
        try:
            if not self.preview:
                self.keyboard.close()
        except KeyboardError as exc:
            self.fail(exc)
        if self.grabbed is not None:
            try:
                if self.config.input_mode == 'bridge':
                    self.grabbed.bridge.resume()
                else:
                    self.grabbed.device.ungrab()
            except OSError:
                pass
            self.grabbed = None
        self.visible_deadline = None

    @contextmanager
    def fetching(self):
        self.loading = True
        self.emit()
        try:
            yield
        finally:
            self.loading = False
            self.emit()

    def refresh(self):
        if self.preview:
            self.emit()
            return
        old_windows = self.desktop.data.get('windows', [])
        old_address = old_windows[self.selected]['address'] if self.selected < len(old_windows) else None
        with self.fetching():
            if self.machine.mode in ('idle', 'arming'):
                self.desktop.refresh(self.config.monitor)
            else:
                self.desktop.refresh()
            windows = self.desktop.data['windows']
            self.selected = next((i for i, w in enumerate(windows) if w['address'] == old_address), 0)

    def open(self):
        try:
            if not self.ui_ready:
                raise DesktopError('The overlay is not ready')
            if not self.preview:
                if not self.desktop.data or self.refresh_at is not None or self.events is None:
                    self.desktop.refresh(self.config.monitor)
                    self.refresh_at = None
                pad = self.pads.get(self.machine.owner)
                if pad is None:
                    raise DesktopError('No compatible controller is connected')
                # Query again immediately before grabbing, rather than trusting a cached frame.
                pad.sync()
                if not pad.state.neutral(self.config.dead_zone):
                    return
                if self.config.input_mode == 'bridge':
                    pad.bridge.pause()
                else:
                    pad.device.grab()
                self.grabbed = pad
            self.selected = 0
            self.error = ''
            self.machine.opened()
            self.visible_deadline = time.monotonic() + 2
            self.emit()
        except (OSError, DesktopError) as exc:
            self.release()
            self.machine.reset()
            self.fail(f'Could not open desktop mode: {exc}')

    def close(self, confirm=False):
        address = self.selected_on_close
        self.selected_on_close = None
        self.release()
        self.machine.reset()
        self.emit()
        if confirm and address and not self.preview:
            try:
                with self.fetching():
                    self.desktop.focus(address)
            except DesktopError as exc:
                self.fail(exc)

    def begin_close(self, confirm=False):
        if self.machine.persistent and self.machine.mode == 'desktop':
            self.begin_view('confirm' if confirm else 'hide')
            return
        windows = self.data().get('windows', [])
        if confirm and not windows:
            return
        self.selected_on_close = windows[self.selected]['address'] if confirm and self.selected < len(windows) else None
        self.machine.request_close('confirm' if confirm else 'cancel')
        self.emit()
        if self.preview:
            self.close(confirm)

    def begin_view(self, action):
        if self.machine.mode != 'desktop':
            return
        if self.machine.pending_view is None:
            if not self.machine.request_view(action):
                return
        elif self.machine.pending_view != action:
            return
        windows = self.data().get('windows', [])
        if action == 'confirm':
            if self.selected >= len(windows):
                self.machine.pending_view = None
                self.emit()
                return
            self.selected_on_close = windows[self.selected]['address']
        if self.pointer:
            self.pointer.release()
        self.emit()
        if self.preview:
            self.commit_view(action)

    def commit_view(self, action):
        try:
            if action == 'confirm':
                address = self.selected_on_close
                self.selected_on_close = None
                if not address:
                    raise DesktopError('That window is no longer available')
                if not self.preview:
                    with self.fetching():
                        self.desktop.focus(address)
            if action == 'switcher':
                if not self.preview:
                    if self.refresh_at is not None or self.events is None:
                        self.desktop.refresh()
                        self.refresh_at = None
                    self.keyboard.close()
                self.machine.viewed('switcher')
                self.visible_deadline = time.monotonic() + 2
            else:
                if self.pointer is None and not self.preview:
                    self.pointer = DesktopPointer(self.config)
                self.machine.viewed('pointer')
                self.visible_deadline = None
                if action in ('keyboard', 'keyboard_toggle') and not self.preview:
                    if action == 'keyboard':
                        self.keyboard.open()
                    else:
                        self.keyboard.toggle()
            self.error = ''
            self.emit()
        except (OSError, UInputError, DesktopError, KeyboardError) as exc:
            self.machine.pending_view = None
            self.machine.previous.clear()
            self.fail(exc)

    def resume(self):
        if self.machine.mode in ('desktop', 'arming'):
            self.selected_on_close = None
            self.machine.request_close('resume')
            if self.pointer:
                self.pointer.release()
            self.emit()
            if self.preview:
                self.close()

    def pointer_active(self):
        return (self.pointer is not None and self.machine.mode == 'desktop' and
                self.machine.view == 'pointer' and self.machine.pending_view is None)

    def tick_pad(self, path, pad, now):
        for action, value in self.machine.tick(path, pad.state, now):
            self.action(action, value)
        if self.machine.owner == path and self.pointer_active():
            self.pointer.sync_buttons(pad.state)

    def navigate(self, direction):
        count = len(self.data().get('windows', []))
        if not count:
            return
        delta = {'left': -1, 'right': 1, 'up': -self.columns, 'down': self.columns}.get(direction, 0)
        self.selected = max(0, min(count - 1, self.selected + delta))
        self.emit()

    def action(self, action, value):
        if action == 'armed':
            self.emit()
        elif action == 'open':
            self.open()
        elif action == 'workspace':
            try:
                if self.preview:
                    workspaces = self.preview_data['workspaces']
                    number = self.preview_data['workspace']['id']
                    self.preview_data['workspace'] = workspaces[(number - 1 + value) % len(workspaces)]
                else:
                    with self.fetching():
                        self.desktop.switch(value)
                self.selected = 0
                self.emit()
            except DesktopError as exc:
                self.fail(exc)
        elif action == 'navigate':
            self.navigate(value)
        elif action == 'view_pending':
            self.begin_view(value)
        elif action == 'view':
            self.commit_view(value)
        elif action == 'closing':
            if self.pointer:
                self.pointer.release()
            # The pure state machine has already entered closing.
            windows = self.data().get('windows', [])
            confirm = self.machine.exit_action == 'confirm'
            if confirm and not windows:
                self.machine.opened()
                return
            self.selected_on_close = windows[self.selected]['address'] if confirm and self.selected < len(windows) else None
            self.emit()
        elif action == 'close':
            self.close(value == 'confirm')
        elif action == 'disconnect':
            self.selected_on_close = None
            self.release()
            self.emit()

    def remove_pad(self, path):
        pad = self.pads.pop(path)
        for action, value in self.machine.remove(path):
            self.action(action, value)
        self.selector.unregister(pad.device.fd)
        if self.config.input_mode == 'bridge':
            pad.bridge.detach()
        pad.device.close()
        self.emit()

    def scan(self):
        paths = {str(p) for p in Path('/dev/input').glob('event*')}
        errors = []
        changed = False
        self.ignored_devices = {path: record for path, record in self.ignored_devices.items()
                                if path in paths}
        for path in list(self.pads):
            if path not in paths:
                self.remove_pad(path)
                changed = True
        for path in sorted(paths - self.pads.keys()):
            stamp = device_stamp(path)
            cached = self.ignored_devices.get(path)
            if stamp is not None and cached is not None and cached[0] == stamp:
                if cached[1]:
                    errors.append(cached[1])
                continue
            self.ignored_devices.pop(path, None)
            device = None
            try:
                device = InputDevice(path)
                caps = device.capabilities()
                keys = caps.get(e.EV_KEY, [])
                if device.name.startswith(PREFIX) or not compatible_gamepad(caps):
                    self.ignored_devices[path] = (stamp, '')
                    device.close()
                    continue
                if not self.config.gesture <= set(keys):
                    reason = f'{device.name}: missing activation buttons'
                    errors.append(reason)
                    self.ignored_devices[path] = (stamp, reason)
                    device.close()
                    continue
                if self.config.controller != 'auto' and self.config.controller not in (path, device.name):
                    self.ignored_devices[path] = (stamp, '')
                    device.close()
                    continue
                pad = Pad(device)
                if self.config.input_mode == 'bridge':
                    key = identity(device)
                    bridge = next((b for b in self.bridges if b.key == key and b.source is None), None)
                    if bridge is not None:
                        bridge.attach(device)
                    else:
                        bridge = Bridge(device)
                        self.bridges.append(bridge)
                        self.selector.register(bridge.ui.fd, selectors.EVENT_READ, ('feedback', bridge))
                    pad.bridge = bridge
                self.pads[path] = pad
                self.selector.register(device.fd, selectors.EVENT_READ, ('pad', path))
                changed = True
            except OSError as exc:
                if device is not None:
                    device.close()
                # Only report inaccessible nodes known to be gamepads.
                props = Path('/run/udev/data') / f'c13:{64 + int(Path(path).name.removeprefix("event"))}'
                try:
                    is_pad = 'E:ID_INPUT_JOYSTICK=1' in props.read_text()
                except OSError:
                    is_pad = False
                if is_pad:
                    errors.append(f'{path}: {exc.strerror}; joystick access is required')
        if errors != self.scan_errors:
            changed = True
        self.scan_errors = errors
        if changed:
            self.emit()

    def connect_events(self):
        try:
            self.events = event_socket()
            self.selector.register(self.events, selectors.EVENT_READ, ('events', None))
        except (OSError, DesktopError):
            self.events = None
        self.event_retry_at = time.monotonic() + 2

    def command(self, message):
        command = message.get('command')
        if command == 'heartbeat':
            self.last_heartbeat = time.monotonic()
            self.ui_ready = True
        elif command == 'visible':
            self.visible_deadline = None
        elif command == 'layout':
            self.columns = max(1, min(4, int(message.get('columns', 2))))
        elif command == 'status':
            pass
        elif command == 'stop':
            self.running = False
        elif command == 'resume':
            self.resume()
        elif command == 'toggle':
            if self.machine.persistent and self.machine.mode == 'desktop':
                self.begin_view('hide' if self.machine.view == 'switcher' else 'switcher')
            elif self.machine.mode != 'idle':
                self.begin_close()
            else:
                owner = next(iter(self.pads), 'preview' if self.preview else None)
                if owner is None:
                    self.fail('No compatible gamepad found. Connect a controller or start a Moonlight stream.')
                else:
                    self.machine.request_open(owner)
                    self.emit()
                    if self.preview:
                        self.open()
        elif command in ('cancel', 'confirm') and self.machine.mode == 'desktop' and self.machine.view == 'switcher':
            self.begin_close(command == 'confirm')
        elif command == 'navigate' and self.machine.mode == 'desktop' and self.machine.view == 'switcher':
            self.navigate(message.get('direction'))
        elif command in ('select', 'highlight') and self.machine.mode == 'desktop' and self.machine.view == 'switcher':
            index = int(message.get('index', -1))
            if 0 <= index < len(self.data().get('windows', [])):
                self.selected = index
                if command == 'select':
                    self.begin_close(True)
                else:
                    self.emit()
        elif command == 'workspace' and self.machine.mode == 'desktop':
            delta = int(message.get('delta', 0))
            if delta in (-1, 1):
                self.action('workspace', delta)
        elif command == 'preview-empty' and self.preview:
            self.preview_data['windows'] = []
            self.emit()
        elif command not in ('cancel', 'confirm', 'navigate', 'select', 'highlight', 'workspace'):
            raise ValueError('Unknown command')
        return self.state()

    def start(self):
        self.lock = (runtime_dir() / ('preview.lock' if self.preview else 'instance.lock')).open('a')
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('hyprcontroller is already running')
        self.path.unlink(missing_ok=True)
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.socket.bind(str(self.path))
        self.owns_socket = True
        self.path.chmod(0o600)
        self.socket.listen(8)
        self.socket.setblocking(False)
        self.selector.register(self.socket, selectors.EVENT_READ, ('listen', None))
        os.set_blocking(sys.stdin.fileno(), False)
        self.selector.register(sys.stdin, selectors.EVENT_READ, ('stdin', None))
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: setattr(self, 'running', False))
        if not self.preview:
            self.connect_events()
            configure_overlay(True)
            self.overlay_configured = True
            try:
                self.desktop.refresh(self.config.monitor)
            except DesktopError as exc:
                self.fail(exc)
        self.emit()

    def disconnect_client(self, conn):
        self.selector.unregister(conn)
        conn.close()
        self.clients.pop(conn, None)

    def dispatch_io(self, kind, value, fileobj):
        if kind == 'listen':
            conn, _ = self.socket.accept()
            conn.setblocking(False)
            self.clients[conn] = (b'', time.monotonic())
            self.selector.register(conn, selectors.EVENT_READ, ('client', conn))
        elif kind == 'stdin':
            chunk = os.read(sys.stdin.fileno(), 8192)
            if not chunk:
                self.running = False
                return
            self.stdin_buffer += chunk
            if len(self.stdin_buffer) > 65536:
                self.running = False
                return
            while b'\n' in self.stdin_buffer:
                line, self.stdin_buffer = self.stdin_buffer.split(b'\n', 1)
                try:
                    self.command(json.loads(line))
                except (ValueError, TypeError, AttributeError) as exc:
                    self.fail(f'Invalid overlay command: {exc}')
        elif kind == 'client':
            conn = value
            try:
                chunk = conn.recv(8192)
                if not chunk:
                    self.disconnect_client(conn)
                    return
                buffer, started = self.clients[conn]
                buffer += chunk
                if len(buffer) > 65536:
                    self.disconnect_client(conn)
                    return
                self.clients[conn] = (buffer, started)
                if b'\n' in buffer:
                    try:
                        message = json.loads(buffer.split(b'\n', 1)[0])
                        # UI readiness can only be asserted by the owned stdin pipe.
                        if message.get('command') in ('heartbeat', 'visible', 'layout'):
                            raise ValueError('Overlay-only command')
                        response = self.command(message)
                    except (ValueError, TypeError, AttributeError) as exc:
                        response = {'error': str(exc)}
                    conn.settimeout(0.2)
                    conn.sendall((json.dumps(response) + '\n').encode())
                    self.disconnect_client(conn)
            except OSError:
                if conn in self.clients:
                    self.disconnect_client(conn)
        elif kind == 'events':
            chunk = self.events.recv(65536)
            if not chunk:
                self.selector.unregister(self.events)
                self.events.close()
                self.events = None
                if self.machine.mode != 'idle':
                    self.close()
                return
            self.event_buffer += chunk
            if len(self.event_buffer) > 65536:
                self.event_buffer = b''
            while b'\n' in self.event_buffer:
                line, self.event_buffer = self.event_buffer.split(b'\n', 1)
                name = line.split(b'>>', 1)[0]
                if name == b'configreloaded':
                    configure_overlay(True)
                if name in (b'workspace', b'workspacev2', b'focusedmon', b'focusedmonv2', b'activewindow',
                            b'activewindowv2', b'openwindow', b'closewindow', b'movewindow', b'movewindowv2',
                            b'windowtitle', b'windowtitlev2', b'monitorremoved', b'monitoradded', b'fullscreen'):
                    if self.refresh_at is None:
                        self.refresh_at = time.monotonic()
        elif kind == 'feedback':
            try:
                for event in value.ui.read():
                    try:
                        value.feedback(event)
                    except (OSError, UInputError) as exc:
                        self.fail(f'Controller feedback failed: {exc}')
            except BlockingIOError:
                pass
            except OSError as exc:
                self.fail(f'Controller feedback read failed: {exc}')
        elif kind == 'pad':
            pad = self.pads.get(value)
            if pad is None:
                return
            try:
                for event in pad.device.read():
                    if event.type == e.EV_SYN and event.code == e.SYN_DROPPED:
                        # Losing events during a grab can leave the application out of sync.
                        if self.machine.owner == value:
                            self.close()
                        pad.sync()
                        if self.config.input_mode == 'bridge':
                            pad.bridge.neutralize()
                    else:
                        pad.state.update(event)
                        if self.config.input_mode == 'bridge':
                            pad.bridge.forward(event)
                        if event.type == e.EV_SYN and event.code == e.SYN_REPORT and self.ui_ready:
                            # Preserve press/release edges even if several frames arrive together.
                            self.tick_pad(value, pad, time.monotonic())
            except BlockingIOError:
                pass
            except OSError:
                self.remove_pad(value)

    def run(self):
        try:
            self.start()
            while self.running:
                for key, _ in self.selector.select(0.016):
                    self.dispatch_io(*key.data, key.fileobj)
                now = time.monotonic()
                if now - self.last_heartbeat > 3:
                    self.fail('Overlay stopped responding; releasing the controller')
                    break
                if self.visible_deadline is not None and now > self.visible_deadline:
                    self.close()
                    self.fail('Overlay did not become visible; controller released')
                if not self.preview and now >= self.scan_at:
                    self.scan()
                    self.scan_at = now + 1
                if self.ui_ready:
                    for path, pad in list(self.pads.items()):
                        self.tick_pad(path, pad, now)
                    if self.pointer_active():
                        pad = self.pads.get(self.machine.owner)
                        if pad is not None:
                            self.pointer.update(pad.state, now)
                if not self.preview and self.events is None and now >= self.event_retry_at:
                    self.connect_events()
                    self.refresh_at = now
                if self.refresh_at is not None and now >= self.refresh_at:
                    self.refresh_at = None
                    try:
                        self.refresh()
                    except DesktopError as exc:
                        if self.machine.mode == 'desktop':
                            self.close()
                        self.desktop.data = {}
                        self.fail(exc)
                for conn, (_, started) in list(self.clients.items()):
                    if now - started > 2:
                        self.disconnect_client(conn)
        finally:
            self.release()
            if self.overlay_configured:
                try:
                    configure_overlay(False)
                except DesktopError:
                    pass
            for bridge in self.bridges:
                bridge.close()
            for pad in self.pads.values():
                pad.device.close()
            for conn in self.clients:
                conn.close()
            if self.events:
                self.events.close()
            if self.socket:
                self.socket.close()
            if self.owns_socket:
                self.path.unlink(missing_ok=True)
            self.selector.close()
            if self.lock:
                self.lock.close()


def main():
    try:
        config = Config.load(os.environ.get('HYPRCONTROLLER_CONFIG'))
        if os.environ.get('HYPRCONTROLLER_BRIDGE') == '1':
            from dataclasses import replace
            config = replace(config, input_mode='bridge')
        Service(config, os.environ.get('HYPRCONTROLLER_PREVIEW') == '1').run()
    except (ValueError, RuntimeError, OSError, DesktopError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
