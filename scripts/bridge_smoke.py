#!/usr/bin/env python3
"""Exercise the real virtual-controller bridge and isolated game device view."""
import json
import argparse
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from evdev import AbsInfo, InputDevice, UInput, ecodes as e
from hyprcontroller.cli import request
from hyprcontroller.game import build_command
from live_smoke import status, wait_for, gesture, send, assert_released, ensure_no_steam


PROBE = '''
import json,sys,select
from pathlib import Path
from evdev import InputDevice,ff,ecodes as e
d=InputDevice(sys.argv[1])
d.grab()
for line in sys.stdin:
    cmd=line.strip()
    if cmd=='view':
        result={'inputs':[str(p) for p in Path('/dev/input').iterdir()],
                'hidraw':[str(p) for p in Path('/dev').glob('hidraw*')],
                'usb':Path('/dev/bus/usb').exists(),'uinput':Path('/dev/uinput').exists()}
    elif cmd=='state':
        result={'buttons':d.active_keys(),'x':d.absinfo(e.ABS_X).value,'hat':d.absinfo(e.ABS_HAT0X).value}
    elif cmd=='events':
        result=[]
        while select.select([d.fd],[],[],0)[0]:
            result += [(v.type,v.code,v.value) for v in d.read()]
    elif cmd=='rumble':
        effect=ff.Effect(e.FF_RUMBLE,-1,0,ff.Trigger(0,0),ff.Replay(100,0),ff.EffectType(ff_rumble_effect=ff.Rumble(1000,1000)))
        eid=d.upload_effect(effect); d.write(e.EV_FF,eid,1); result={'effect':eid}
    else:
        break
    print(json.dumps(result),flush=True)
'''


def run(runtime=None):
    ensure_no_steam()
    try:
        status()
    except OSError:
        pass
    else:
        raise RuntimeError('Stop the running overlay before this isolated test')
    caps = {e.EV_KEY: [e.BTN_SOUTH, e.BTN_EAST, e.BTN_NORTH, e.BTN_WEST, e.BTN_START,
                        e.BTN_TL, e.BTN_TR, e.BTN_THUMBL, e.BTN_THUMBR],
            e.EV_ABS: [(e.ABS_X, AbsInfo(0, -32768, 32767, 0, 0, 0)),
                       (e.ABS_Y, AbsInfo(0, -32768, 32767, 0, 0, 0)),
                       (e.ABS_HAT0X, AbsInfo(0, -1, 1, 0, 0, 0)),
                       (e.ABS_RX, AbsInfo(0, -32768, 32767, 0, 0, 0)),
                       (e.ABS_RY, AbsInfo(0, -32768, 32767, 0, 0, 0))],
            e.EV_FF: [e.FF_RUMBLE]}
    feedback = []
    feedback_stop = threading.Event()
    app = probe = ui = mouse = None

    def hardware_feedback(device):
        while not feedback_stop.is_set():
            try:
                if not select.select([device.fd], [], [], .05)[0]:
                    continue
                for event in device.read():
                    if event.type == e.EV_UINPUT and event.code == e.UI_FF_UPLOAD:
                        upload = device.begin_upload(event.value)
                        upload.retval = 0
                        device.end_upload(upload)
                        feedback.append(('upload', upload.effect.id))
                    elif event.type == e.EV_UINPUT and event.code == e.UI_FF_ERASE:
                        erase = device.begin_erase(event.value)
                        erase.retval = 0
                        device.end_erase(erase)
                    elif event.type == e.EV_FF:
                        feedback.append(('play', event.code, event.value))
            except (OSError, ValueError):
                return

    def source():
        device = UInput(caps, name='hyprcontroller bridge test source', phys='hyprcontroller/smoke',
                        vendor=0x045e, product=0x028e, max_effects=8)
        threading.Thread(target=hardware_feedback, args=(device,), daemon=True).start()
        return device

    def ask(command):
        probe.stdin.write(command + '\n'); probe.stdin.flush()
        if not select.select([probe.stdout], [], [], 5)[0]:
            raise RuntimeError('Game-view probe did not respond: ' + command)
        return json.loads(probe.stdout.readline())

    with tempfile.TemporaryDirectory(prefix='hyprcontroller-bridge-') as tmp:
        config = Path(tmp) / 'config.json'
        config.write_text(json.dumps({'controller': 'hyprcontroller bridge test source', 'input_mode': 'bridge'}))
        log = (Path(tmp) / 'app.log').open('w+')
        try:
            ui = source()
            raw_path = ui.device.path
            app = subprocess.Popen([str(ROOT/'bin/hyprcontroller'), 'run', '--config', str(config)],
                                   stdout=log, stderr=log, start_new_session=True)
            wait_for(lambda: status()['virtual_controllers'], 'bridge ready')
            s = status()
            virtual_path = s['virtual_controllers'][0]['path']
            probe = subprocess.Popen(build_command([sys.executable, '-u', '-c', PROBE, virtual_path], s),
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
            view = ask('view')
            assert virtual_path in view['inputs'] and raw_path not in view['inputs']
            assert not view['hidraw'] and not view['usb'] and not view['uinput']
            print('Game sees only virtual inputs, no raw HID/USB/uinput: passed', flush=True)
            if runtime:
                nested = build_command([str(runtime), '--', '/bin/sh', '-c',
                                        'test -c "$1" && ! test -d /dev/bus/usb && ! test -e /dev/uinput && '
                                        'for node in /dev/hidraw*; do test ! -e "$node" || exit 1; done',
                                        'probe', virtual_path], s)
                subprocess.run(nested, check=True, timeout=40, stdout=log, stderr=log)
                print('Nested Steam Linux Runtime preserves the isolated device view: passed', flush=True)
            send(ui, e.ABS_X, 22000, e.EV_ABS)
            assert ask('state')['x'] == 22000
            send(ui, e.ABS_X, 0, e.EV_ABS)
            ask('rumble')
            wait_for(lambda: any(v[0]=='play' and v[2]==1 for v in feedback), 'rumble forwarded')
            print('Gameplay axes and rumble forwarding: passed', flush=True)
            gesture(ui)
            mouse=InputDevice(status()['pointer_path'])
            # Inspect generated mouse events without clicking or moving the user's desktop.
            mouse.grab()
            ask('events')
            send(ui, e.ABS_HAT0X, 1, e.EV_ABS)
            send(ui, e.ABS_X, 22000, e.EV_ABS)
            send(ui, e.BTN_WEST, 1)
            neutral = ask('state')
            assert not neutral['buttons'] and neutral['x']==0 and neutral['hat']==0
            assert not ask('events'), 'Overlay navigation leaked into virtual game input'
            send(ui, e.BTN_WEST, 0)
            send(ui, e.ABS_X, 0, e.EV_ABS)
            send(ui, e.ABS_HAT0X, 0, e.EV_ABS)
            print('Overlay blocks event readers AND button/axis state queries: passed', flush=True)
            send(ui, e.BTN_EAST, 1)
            assert status()['mode']=='desktop' and status()['view_pending']=='hide'
            send(ui, e.BTN_EAST, 0)
            wait_for(lambda: status()['view']=='pointer', 'persistent desktop view')
            send(ui, e.ABS_X, 22000, e.EV_ABS)
            send(ui, e.ABS_RY, 32767, e.EV_ABS)
            send(ui, e.BTN_SOUTH, 1)
            time.sleep(.2)
            generated=[]
            while select.select([mouse.fd],[],[],0)[0]:
                generated.extend(mouse.read())
            assert any(v.type==e.EV_REL and v.code==e.REL_X for v in generated)
            assert any(v.type==e.EV_REL and v.code==e.REL_WHEEL for v in generated)
            assert any(v.type==e.EV_KEY and v.code==e.BTN_LEFT and v.value==1 for v in generated)
            assert not ask('state')['buttons'] and ask('state')['x']==0
            assert not ask('events'), 'Pointer-view input reached the game'
            send(ui, e.BTN_SOUTH, 0)
            send(ui, e.ABS_X, 0, e.EV_ABS)
            send(ui, e.ABS_RY, 0, e.EV_ABS)
            send(ui, e.BTN_START, 1); send(ui, e.BTN_START, 0)
            try:
                wait_for(lambda: status()['view']=='switcher', 'reopen switcher')
            except RuntimeError:
                print('Failed handoff state:', status(), flush=True)
                raise
            assert status()['mode']=='desktop'
            print('Persistent desktop capture, mouse, scrolling, and switcher reopening: passed', flush=True)
            send(ui, e.BTN_THUMBL, 1); send(ui, e.BTN_THUMBR, 1)
            time.sleep(1.1)
            assert status()['mode']=='closing'
            send(ui, e.BTN_THUMBL, 0); send(ui, e.BTN_THUMBR, 0)
            wait_for(lambda: status()['mode']=='idle', 'explicit gameplay return')
            mouse.close(); mouse=None
            assert not ask('state')['buttons']
            send(ui, e.BTN_SOUTH, 1)
            assert e.BTN_SOUTH in ask('state')['buttons']
            send(ui, e.BTN_SOUTH, 0)
            print('Neutral handoff and gameplay resume: passed', flush=True)
            gesture(ui)
            ui.close(); ui=None
            wait_for(lambda: status()['mode']=='idle', 'disconnect closes overlay')
            wait_for(lambda: not status()['virtual_controllers'][0]['connected'], 'disconnected slot')
            assert not ask('state')['buttons']
            ui=source()
            wait_for(lambda: status()['virtual_controllers'][0]['connected'], 'reconnected slot')
            assert status()['virtual_controllers'][0]['path']==virtual_path
            send(ui, e.ABS_X, 24000, e.EV_ABS)
            assert ask('state')['x']==24000
            send(ui, e.ABS_X, 0, e.EV_ABS)
            print('Disconnect/reconnect preserves the game-visible device: passed', flush=True)
            gesture(ui)
            raw_path=ui.device.path
            os.kill(status()['pid'], signal.SIGKILL)
            app.wait(timeout=5); app=None
            wait_for(lambda: (assert_released(raw_path) or True), 'crash releases source')
            print('Backend crash releases original controller: passed', flush=True)
            app = subprocess.Popen([str(ROOT/'bin/hyprcontroller'), 'run', '--config', str(config)],
                                   stdout=log, stderr=log, start_new_session=True)
            wait_for(lambda: status()['virtual_controllers'], 'bridge restarted')
            gesture(ui)
            os.kill(app.pid, signal.SIGKILL)
            app.wait(timeout=5); app=None
            wait_for(lambda: (assert_released(raw_path) or True), 'UI crash releases source')
            print('Overlay crash releases original controller: passed', flush=True)
            log.seek(0)
            warnings=[s for s in log.read().splitlines() if 'WARN' in s or 'ERROR' in s]
            if warnings:
                raise RuntimeError('\n'.join(warnings))
        finally:
            if mouse:
                mouse.close()
            if probe:
                probe.stdin.close()
                probe.wait(timeout=5)
            try:
                request({'command':'stop'})
            except (OSError, RuntimeError):
                pass
            if app:
                try:
                    app.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    app.terminate(); app.wait(timeout=5)
            feedback_stop.set()
            if ui:
                ui.close()
            log.seek(0)
            Path('/tmp/hyprcontroller-bridge-smoke.log').write_text(log.read())
            log.close()


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime', type=Path, help='Optional Steam Linux Runtime run executable')
    run(parser.parse_args().runtime)
