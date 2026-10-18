import json
import os
from pathlib import Path
import re
import socket
import subprocess


class DesktopError(RuntimeError):
    pass


def hyprctl(*args):
    try:
        result = subprocess.run(['hyprctl', *args], capture_output=True, text=True, timeout=2)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DesktopError(f'Cannot reach Hyprland: {exc}') from exc
    if result.returncode:
        raise DesktopError(result.stderr.strip() or result.stdout.strip() or 'Hyprland command failed')
    return result.stdout


def query(name):
    try:
        return json.loads(hyprctl('-j', name))
    except json.JSONDecodeError as exc:
        raise DesktopError('Hyprland returned an invalid response') from exc


def query_snapshot():
    reply = hyprctl('-j', '--batch', 'monitors;workspaces;clients;activewindow').lstrip()
    decoder = json.JSONDecoder()
    values = []
    try:
        while reply:
            value, end = decoder.raw_decode(reply)
            values.append(value)
            reply = reply[end:].lstrip()
    except json.JSONDecodeError as exc:
        raise DesktopError('Hyprland returned an invalid snapshot') from exc
    if len(values) != 4 or any(not isinstance(v, list) for v in values[:3]) or not isinstance(values[3], dict):
        raise DesktopError('Hyprland returned an incomplete snapshot')
    return values


def configure_overlay(enabled):
    hyprctl('eval', 'hl.layer_rule({ name = "hyprcontroller-overlay", enabled = ' +
            ('true' if enabled else 'false') + ', match = { namespace = "^hyprcontroller$" }, ' +
            'blur = false, xray = false, ignore_alpha = 0, no_anim = true })')


def lua_string(value):
    # JSON quoting is valid for these UTF-8 Lua strings; neutralize all controls.
    text = str(value)
    if any(ord(c) < 32 for c in text):
        raise DesktopError('Invalid workspace or monitor name')
    return json.dumps(text, ensure_ascii=False)


def workspace_list(workspaces, monitor_name, monitor_id, current):
    eligible = {}
    occupied_elsewhere = set()
    for ws in workspaces:
        if ws['id'] <= 0 or ws['name'].startswith('special:'):
            continue
        if ws.get('monitor') == monitor_name or ws.get('monitorID') == monitor_id:
            eligible[ws['name']] = {'id': ws['id'], 'name': ws['name']}
        else:
            occupied_elsewhere.add(ws['name'])
    for number in range(1, 11):
        name = str(number)
        if name not in occupied_elsewhere:
            eligible.setdefault(name, {'id': number, 'name': name})
    if current['id'] > 0:
        eligible.setdefault(current['name'], {'id': current['id'], 'name': current['name']})
    return sorted(eligible.values(), key=lambda ws: (ws['id'], ws['name']))


class Desktop:
    def __init__(self):
        self.monitor = ''
        self.data = {}

    def refresh(self, monitor=None):
        monitors, all_workspaces, all_clients, active_window = query_snapshot()
        monitors = [m for m in monitors if not m.get('disabled')]
        desired = monitor if monitor is not None else self.monitor
        if desired:
            chosen = next((m for m in monitors if m['name'] == desired), None)
            if chosen is None:
                raise DesktopError(f'Monitor {desired} is unavailable')
        else:
            chosen = next((m for m in monitors if m.get('focused')), monitors[0] if monitors else None)
        if chosen is None:
            raise DesktopError('No active monitor')
        self.monitor = chosen['name']
        current = chosen['activeWorkspace']
        workspaces = workspace_list(all_workspaces, chosen['name'], chosen['id'], current)
        clients = [c for c in all_clients if c['workspace']['id'] == current['id']
                   and c.get('mapped', True) and not c.get('hidden', False)]
        active = active_window.get('address', '')
        windows = [{'address': c['address'], 'title': c.get('title') or c.get('class') or 'Untitled window',
                    'app': c.get('class') or c.get('initialClass') or 'Application',
                    'active': c['address'] == active} for c in clients]
        windows.sort(key=lambda c: (not c['active'], c['app'].casefold(), c['title'].casefold(), c['address']))
        self.data = {'monitor': chosen['name'], 'workspace': current,
                     'workspaces': workspaces, 'windows': windows}
        return self.data

    def switch(self, delta):
        self.refresh()
        workspaces = self.data['workspaces']
        current = self.data['workspace']['name']
        index = next((i for i, w in enumerate(workspaces) if w['name'] == current), 0)
        target = workspaces[(index + delta) % len(workspaces)]
        hyprctl('eval', 'hl.dispatch(hl.dsp.focus({ monitor = ' + lua_string(self.monitor) + ' })); ' +
                'hl.dispatch(hl.dsp.focus({ workspace = ' + lua_string(target['name']) + ' }))')
        return self.refresh()

    def focus(self, address):
        if not re.fullmatch(r'0x[0-9a-fA-F]+', address):
            raise DesktopError('Invalid window address')
        self.refresh()
        if not any(w['address'] == address for w in self.data['windows']):
            raise DesktopError('That window is no longer available')
        hyprctl('eval', 'hl.dispatch(hl.dsp.focus({ window = ' + lua_string('address:' + address) + ' }))')


def event_socket():
    runtime = Path(os.environ.get('XDG_RUNTIME_DIR', f'/run/user/{os.getuid()}'))
    signature = os.environ.get('HYPRLAND_INSTANCE_SIGNATURE', '')
    if not signature or '/' in signature:
        raise DesktopError('Run hyprcontroller inside your Hyprland session')
    path = runtime / 'hypr' / signature / '.socket2.sock'
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(2)
    try:
        sock.connect(str(path))
    except OSError:
        sock.close()
        raise
    sock.setblocking(False)
    return sock
