#!/usr/bin/env python3
"""Exercise controller events with a temporary gamepad in a live Hyprland session.

This does not replace testing actual hardware gameplay or a Moonlight session.
"""
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from evdev import AbsInfo, InputDevice, UInput, ecodes as e
from hyprcontroller.cli import request
from hyprcontroller.desktop import hyprctl, query


def wait_for(predicate, label, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            value = predicate()
            if value:
                return value
        except (OSError, RuntimeError, KeyError):
            pass
        time.sleep(.03)
    raise RuntimeError(f'Timed out: {label}')


def status():
    return request({'command': 'status'})


def drain(device):
    events = []
    while select.select([device.fd], [], [], 0)[0]:
        events.extend(device.read())
    return events


def send(ui, code, value, kind=e.EV_KEY):
    ui.write(kind, code, value)
    ui.syn()
    time.sleep(.08)


def gesture(ui):
    send(ui, e.BTN_THUMBL, 1)
    send(ui, e.BTN_THUMBR, 1)
    time.sleep(1.1)
    wait_for(lambda: status()['mode'] == 'arming', 'gesture armed')
    send(ui, e.BTN_THUMBL, 0)
    send(ui, e.BTN_THUMBR, 0)
    wait_for(lambda: status()['mode'] == 'desktop', 'overlay opened')


def assert_released(path):
    probe = InputDevice(path)
    try:
        probe.grab()
        probe.ungrab()
    finally:
        probe.close()


def ensure_no_steam():
    # Temporary gamepads are visible to host launchers, even when the probe is isolated.
    for path in Path('/proc').glob('[0-9]*/comm'):
        try:
            if path.read_text().strip() == 'steam':
                raise RuntimeError('Exit Steam and games before this synthetic gameplay test; test inputs can reach them')
        except (OSError, PermissionError):
            continue


def run():
    ensure_no_steam()
    try:
        request({'command': 'status'})
    except OSError:
        pass
    else:
        raise RuntimeError('Stop the existing prototype before running the isolated live test')
    original = query('activewindow')
    monitors = query('monitors')
    monitor = next(m for m in monitors if m['focused'])
    initial_workspace = monitor['activeWorkspace']['name']
    caps = {
        e.EV_KEY: [e.BTN_SOUTH, e.BTN_EAST, e.BTN_NORTH, e.BTN_WEST,
                   e.BTN_TL, e.BTN_TR, e.BTN_THUMBL, e.BTN_THUMBR],
        e.EV_ABS: [(e.ABS_X, AbsInfo(0, -32768, 32767, 0, 0, 0)),
                   (e.ABS_Y, AbsInfo(0, -32768, 32767, 0, 0, 0)),
                   (e.ABS_HAT0X, AbsInfo(0, -1, 1, 0, 0, 0)),
                   (e.ABS_HAT0Y, AbsInfo(0, -1, 1, 0, 0, 0))]}
    with tempfile.TemporaryDirectory(prefix='hyprcontroller-live-') as tmp:
        config = Path(tmp) / 'config.json'
        config.write_text(json.dumps({'controller': 'hyprcontroller test gamepad', 'monitor': monitor['name']}))
        log = (Path(tmp) / 'app.log').open('w+')
        proc = None
        ui = None
        observer = None
        try:
            ui = UInput(caps, name='hyprcontroller test gamepad', vendor=0x045e, product=0x028e)
            path = ui.device.path
            observer = InputDevice(path)
            proc = subprocess.Popen([str(ROOT / 'bin/hyprcontroller'), 'run', '--config', str(config)],
                                    stdout=log, stderr=log, start_new_session=True)
            wait_for(lambda: status()['controllers'], 'synthetic controller discovered')
            # A short press stays in game mode and is visible to a normal game reader.
            send(ui, e.BTN_THUMBL, 1); send(ui, e.BTN_THUMBR, 1)
            send(ui, e.BTN_THUMBL, 0); send(ui, e.BTN_THUMBR, 0)
            assert status()['mode'] == 'idle'
            assert any(event.code == e.BTN_THUMBL for event in drain(observer))
            print('Short gesture and idle passthrough: passed')
            gesture(ui)
            drain(observer)
            send(ui, e.ABS_HAT0X, 1, e.EV_ABS); send(ui, e.ABS_HAT0X, 0, e.EV_ABS)
            assert not drain(observer), 'Navigation leaked into the game reader'
            print('Exclusive navigation capture: passed')
            before = status()['workspace']['name']
            send(ui, e.BTN_TR, 1); send(ui, e.BTN_TR, 0)
            wait_for(lambda: status()['workspace']['name'] != before, 'workspace switched')
            print('Live workspace switching: passed')
            send(ui, e.BTN_TL, 1); send(ui, e.BTN_TL, 0)
            wait_for(lambda: status()['workspace']['name'] == before, 'workspace restored')
            send(ui, e.BTN_SOUTH, 1)
            assert status()['mode'] == 'closing'
            send(ui, e.BTN_SOUTH, 0)
            wait_for(lambda: status()['mode'] == 'idle', 'confirm handoff')
            assert_released(path)
            print('Confirm and neutral handoff: passed')
            gesture(ui)
            send(ui, e.BTN_EAST, 1)
            assert status()['mode'] == 'closing'
            send(ui, e.BTN_EAST, 0)
            wait_for(lambda: status()['mode'] == 'idle', 'cancel handoff')
            assert_released(path)
            print('Cancel handoff: passed')
            gesture(ui)
            ui.close(); ui = None
            wait_for(lambda: status()['mode'] == 'idle', 'disconnect recovery')
            print('Controller disconnect recovery: passed')
            request({'command': 'stop'})
            proc.wait(timeout=5); proc = None
            observer.close(); observer = None
            # Repeat with a new device and abruptly kill the UI parent.
            ui = UInput(caps, name='hyprcontroller test gamepad', vendor=0x045e, product=0x028e)
            path = ui.device.path
            proc = subprocess.Popen([str(ROOT / 'bin/hyprcontroller'), 'run', '--config', str(config)],
                                    stdout=log, stderr=log, start_new_session=True)
            wait_for(lambda: status()['controllers'], 'reconnected test device discovered')
            assert status()['mode'] == 'idle'
            gesture(ui)
            os.kill(proc.pid, signal.SIGKILL)
            proc.wait(timeout=5)
            wait_for(lambda: (assert_released(path) or True), 'UI crash releases input')
            wait_for(lambda: not (Path(os.environ['XDG_RUNTIME_DIR']) / 'hyprcontroller/control.sock').exists(), 'backend exits after UI crash')
            proc = None
            print('UI crash and reconnect recovery: passed')
            proc = subprocess.Popen([str(ROOT / 'bin/hyprcontroller'), 'run', '--config', str(config)],
                                    stdout=log, stderr=log, start_new_session=True)
            wait_for(lambda: status()['controllers'], 'backend crash test ready')
            gesture(ui)
            os.kill(status()['pid'], signal.SIGKILL)
            proc.wait(timeout=5)
            wait_for(lambda: (assert_released(path) or True), 'backend crash releases input')
            proc = None
            print('Backend crash releases controller: passed')
            log.seek(0)
            output = log.read()
            unexpected = [line for line in output.splitlines() if 'WARN' in line or 'ERROR' in line]
            if unexpected:
                raise RuntimeError('Unexpected application warnings:\n' + '\n'.join(unexpected))
            print('Live synthetic-controller smoke test: passed')
        finally:
            try:
                request({'command': 'stop'})
            except (OSError, RuntimeError):
                pass
            if proc is not None:
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGTERM)
                    proc.wait(timeout=5)
            if observer is not None:
                observer.close()
            if ui is not None:
                ui.close()
            hyprctl('eval', 'hl.dispatch(hl.dsp.focus({ monitor = ' + json.dumps(monitor['name']) + ' }))')
            hyprctl('eval', 'hl.dispatch(hl.dsp.focus({ workspace = ' + json.dumps(initial_workspace) + ' }))')
            if original.get('address'):
                hyprctl('eval', 'hl.dispatch(hl.dsp.focus({ window = ' + json.dumps('address:' + original['address']) + ' }))')
            log.close()


if __name__ == '__main__':
    run()
