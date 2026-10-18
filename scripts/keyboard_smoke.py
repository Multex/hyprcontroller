#!/usr/bin/env python3
"""Check the existing keyboard in a temporary text window, with game input paused.

Coordinates below match this host's English (US) keyboard at scale 1.
No synthetic controller buttons are forwarded to gameplay or Steam.
"""
import argparse
import json
import os
from pathlib import Path
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
from live_smoke import status, wait_for, send, assert_released

TEXT_APP = '''
import json,sys
from pathlib import Path
from PySide6.QtCore import QTimer,QUrl
from PySide6.QtGui import QGuiApplication
from PySide6.QtQuick import QQuickView
app=QGuiApplication([])
view=QQuickView(); view.setTitle('hyprcontroller typing test')
view.setResizeMode(QQuickView.SizeRootObjectToView); view.resize(900,600)
view.setSource(QUrl.fromLocalFile(sys.argv[1])); view.show()
timer=QTimer()
timer.timeout.connect(lambda: Path(sys.argv[2]).write_text(json.dumps({'text':view.rootObject().property('content')})))
timer.start(100)
sys.exit(app.exec())
'''

TEXT_QML = '''
import QtQuick
Rectangle {
    color: "#25202e"
    property alias content: editor.text
    Text { x: 30; y: 20; text: "hyprcontroller keyboard test"; color: "white"; font.pixelSize: 24 }
    TextEdit {
        id: editor
        anchors { fill: parent; margins: 30; topMargin: 70 }
        color: "white"; font.pixelSize: 26; focus: true
        Component.onCompleted: forceActiveFocus()
    }
}
'''


def keyboard_layers():
    return [(name, layer) for name, monitor in query('layers').items()
            for layers in monitor['levels'].values() for layer in layers
            if layer.get('namespace') == 'quickshell:osk']


def cursor():
    return query('cursorpos')


def move(ui, x, y):
    # Steer the real desktop pointer with the implemented stick path.
    deadline = time.monotonic() + 6
    try:
        while time.monotonic() < deadline:
            pos = cursor()
            dx, dy = x - pos['x'], y - pos['y']
            if max(abs(dx), abs(dy)) <= 4:
                return
            for axis, delta in ((e.ABS_X, dx), (e.ABS_Y, dy)):
                value = 0 if abs(delta) <= 4 else int((.24 + .76 * min(1, abs(delta) / 150) ** .5) * 32767)
                ui.write(e.EV_ABS, axis, value if delta >= 0 else -value)
            ui.syn()
            time.sleep(.025)
        raise RuntimeError('Pointer did not reach the test target')
    finally:
        ui.write(e.EV_ABS, e.ABS_X, 0); ui.write(e.EV_ABS, e.ABS_Y, 0); ui.syn()
        time.sleep(.1)


def run(inspect=False):
    try:
        status()
    except OSError:
        pass
    else:
        raise RuntimeError('Stop hyprcontroller before this keyboard test')
    if keyboard_layers():
        raise RuntimeError('Close the existing keyboard before this test')
    original = query('activewindow')
    original_cursor = cursor()
    focused = next(m for m in query('monitors') if m['focused'])
    if focused['scale'] != 1:
        raise RuntimeError('This host-specific keyboard test requires scale 1')
    app = text_app = ui = output = None
    with tempfile.TemporaryDirectory(prefix='hyprcontroller-keyboard-') as tmp:
        directory = Path(tmp)
        config = directory / 'config.json'
        config.write_text(json.dumps({'controller': 'hyprcontroller keyboard test source',
                                     'input_mode': 'bridge', 'monitor': focused['name']}))
        qml = directory / 'Text.qml'; qml.write_text(TEXT_QML)
        text_file = directory / 'text.json'
        log = (directory / 'app.log').open('w+')

        def launch():
            nonlocal app, output
            app = subprocess.Popen([str(ROOT / 'bin/hyprcontroller'), 'run', '--config', str(config)],
                                   stdout=log, stderr=log, start_new_session=True)
            wait_for(lambda: status()['virtual_controllers'], 'bridge ready')
            output = InputDevice(status()['virtual_controllers'][0]['path'])
            output.grab()
            request({'command': 'toggle'})
            wait_for(lambda: status()['mode'] == 'desktop', 'desktop mode')
            own = wait_for(lambda: next((w for w in status()['windows'] if w['title'] == 'hyprcontroller typing test'), None), 'test card')
            request({'command': 'select', 'index': next(i for i, w in enumerate(status()['windows']) if w['address'] == own['address'])})
            wait_for(lambda: status()['view'] == 'pointer', 'pointer view')

        def open_keyboard():
            send(ui, e.BTN_NORTH, 1); send(ui, e.BTN_NORTH, 0)
            layers = wait_for(keyboard_layers, 'existing keyboard opens')
            assert query('activewindow')['title'] == 'hyprcontroller typing test', 'Keyboard stole text focus'
            assert not output.active_keys() and output.absinfo(e.ABS_X).value == 0
            return layers

        try:
            text_app = subprocess.Popen([sys.executable, '-c', TEXT_APP, str(qml), str(text_file)], stdout=log, stderr=log)
            own = wait_for(lambda: next((c for c in query('clients') if c['title'] == 'hyprcontroller typing test'), None), 'typing test window')
            hyprctl('eval', 'hl.dispatch(hl.dsp.focus({ window = ' + json.dumps('address:' + own['address']) + ' }))')
            caps = {e.EV_KEY: [e.BTN_SOUTH, e.BTN_EAST, e.BTN_NORTH, e.BTN_START, e.BTN_THUMBL, e.BTN_THUMBR],
                    e.EV_ABS: [(code, AbsInfo(0, -32768, 32767, 0, 0, 0)) for code in (e.ABS_X, e.ABS_Y, e.ABS_RX, e.ABS_RY)]}
            ui = UInput(caps, name='hyprcontroller keyboard test source', phys='hyprcontroller/keyboard-test')
            launch()
            layers = open_keyboard()
            time.sleep(.5)
            subprocess.run(['grim', '-o', focused['name'], '/tmp/hyprcontroller-keyboard.png'], check=True)
            print('Keyboard geometry:', layers, flush=True)
            print('Existing keyboard opens without stealing test-window focus: passed', flush=True)
            if inspect:
                return
            # The full keyboard has 15 columns of 45px with 5px gaps and 6 rows.
            # The 40px control column and separator precede the 745px key content.
            monitor_name, geometry = layers[0]
            monitor = next(m for m in query('monitors') if m['name'] == monitor_name)
            left = monitor['x'] + (monitor['width'] - 816) / 2 + 61
            top = geometry['y'] + (geometry['h'] - 276.5) / 2

            def click(x, y):
                move(ui, x, y)
                assert query('activewindow')['title'] == 'hyprcontroller typing test', 'Refusing to type into another window'
                send(ui, e.BTN_SOUTH, 1); send(ui, e.BTN_SOUTH, 0)

            def content():
                return json.loads(text_file.read_text())['text']

            click(left + 122.5, top + 159)
            wait_for(lambda: content() == 'a', 'letter typed')
            click(left + 56, top + 209)
            click(left + 122.5, top + 159)
            wait_for(lambda: content() == 'aA', 'shifted letter typed')
            click(left + 697, top + 59)
            wait_for(lambda: content() == 'a', 'backspace')
            click(left + 697, top + 159)
            wait_for(lambda: content() == 'a\n', 'enter')
            assert not output.active_keys() and output.absinfo(e.ABS_X).value == 0
            print('Stick pointer and A-click type letters, Shift, Backspace, and Enter; gamepad stays neutral: passed', flush=True)
            send(ui, e.BTN_NORTH, 1); send(ui, e.BTN_NORTH, 0)
            wait_for(lambda: not keyboard_layers(), 'Y closes keyboard')
            assert status()['mode'] == 'desktop'
            open_keyboard()
            send(ui, e.BTN_START, 1); send(ui, e.BTN_START, 0)
            wait_for(lambda: status()['view'] == 'switcher' and not keyboard_layers(), 'switcher closes keyboard')
            request({'command': 'toggle'})
            wait_for(lambda: status()['view'] == 'pointer', 'hide switcher')
            open_keyboard()
            move(ui, original_cursor['x'], original_cursor['y'])
            raw_path = ui.device.path
            os.kill(status()['pid'], signal.SIGKILL)
            app.wait(timeout=5); app = None
            output.close(); output = None
            wait_for(lambda: not keyboard_layers(), 'backend crash closes keyboard')
            assert_released(raw_path)
            print('Keyboard toggle, switcher handoff, and backend-crash cleanup: passed', flush=True)
            launch(); open_keyboard()
            os.kill(app.pid, signal.SIGKILL)
            app.wait(timeout=5); app = None
            output.close(); output = None
            wait_for(lambda: not keyboard_layers(), 'UI crash closes keyboard')
            wait_for(lambda: (assert_released(raw_path) or True), 'UI crash releases controller')
            print('UI-crash keyboard and controller cleanup: passed', flush=True)
        finally:
            try:
                if ui and status()['view'] == 'pointer':
                    move(ui, original_cursor['x'], original_cursor['y'])
                request({'command': 'stop'})
            except (OSError, RuntimeError):
                pass
            if app:
                try: app.wait(timeout=5)
                except subprocess.TimeoutExpired: app.terminate(); app.wait(timeout=5)
            if output: output.close()
            if ui: ui.close()
            if text_app: text_app.terminate(); text_app.wait(timeout=5)
            if original.get('address'):
                hyprctl('eval', 'hl.dispatch(hl.dsp.focus({ window = ' + json.dumps('address:' + original['address']) + ' }))')
            log.seek(0)
            Path('/tmp/hyprcontroller-keyboard-smoke.log').write_text(log.read())
            log.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--inspect', action='store_true', help='Capture the keyboard without clicking keys')
    run(parser.parse_args().inspect)
