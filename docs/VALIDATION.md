# Testing and compatibility

Tests and user reports collected from 7 to 17 October 2026 on one CachyOS desktop running Hyprland. They support an early testing release, with the limits below. The current automated suite contains 90 tests.

## Tested setup

| Component | Setup |
| --- | --- |
| Desktop | Hyprland 0.56.2, using Lua dispatch commands |
| Overlay | Quickshell 0.2.1, illogical-impulse build |
| Python / input library | Current automated checks: Python 3.14.8 and python-evdev 2.0.0 |
| Keyboard provider | end4-pC / illogical-impulse, US layout |
| Local controller | Microsoft Xbox 360 controller |
| Remote controller | Sunshine forwarding from mobile Moonlight; Xbox Series virtual device |
| Displays | 1920×1080 and 1366×768, scale 1 |
| Games / launcher | Multiple games, Steam Big Picture, Omikuji |
| Optional device isolation | Bubblewrap; nested Steam Linux Runtime 4 probe |

## Results

| Check | Evidence |
| --- | --- |
| Gesture timing, neutral handoffs, dead zones, and held navigation | Automated recorded-input tests pass |
| One workspace switch per LB/RB press | Long holds, repeat events, input-state resets, and release/repress covered by regression tests |
| Persistent capture after A/B and workspace changes | Automated and live synthetic checks pass; working behavior reported in use |
| Local and Moonlight navigation during gameplay | Reported working across multiple games and Steam Big Picture; individual trials were not logged separately |
| Circular-stick mouse movement | Smoother motion and continued desktop mode confirmed after the discovery-cache fix |
| Sunshine disconnect/reconnect | Reported working after the rumble-error fix, with a delay of a few seconds before reopening |
| Game-visible virtual input during desktop mode | Live bridge probe observed neutral button/axis state |
| Pointer movement, dragging, and scrolling | Automated tests and live generated-input checks pass |
| Existing keyboard typing and cleanup | Disposable text-window trial covered letter input, Shift, Backspace, Enter, and view/crash cleanup |
| Window disappearance and monitor filtering | Failed focus keeps the picker and capture; workspace eligibility covered by tests |
| Backend/UI failure cleanup | Live tests confirmed original controller access released and virtual mouse removed |
| Rumble forwarding | Simulated force-feedback upload/playback checks pass; actual hardware vibration still needs a separate trial |
| Optional device isolation | Original input, HID, USB, and uinput nodes absent from the launcher device view; nested runtime probe passed |
| UI layout | Fourteen offscreen scenarios passed, including narrow/short screens, empty/loading states, panel centering, and twenty-window focus traversal |
| Backup, install, update, rollback | Scratch extraction, metadata checks, later-edit protection, and isolated installation tests pass |

Input leakage was observed with the original observe-mode grab, both locally and through Moonlight. Bridge mode resolved the reported leak. Persistent desktop mode then kept capture after window and workspace changes. Some games can still bypass source-device grabs and need the isolated launcher.

### Regression measurements

Periodic discovery originally reopened rejected input nodes every second. On this host, caching those results reduced median steady discovery time from 145 ms to 0.2 ms. In a 3.2-second probe per version with a 16 ms input-loop wait, the largest gap fell from 158 ms to 16.3 ms, with no gaps above 50 ms after the change. These measurements isolate discovery cost and do not measure streamed cursor latency.

A later Sunshine reconnect report exposed an uncaught python-evdev `UInputError` during a rumble upload. The backend now catches feedback errors per request and continues with later events in the same batch. Regression tests cover failed begin/end upload and erase requests, plus a disconnected feedback reader. The old backend fails those handshake cases; the patched version passed the subsequent user reconnect trial.

## Known limits

- Initial activation-button presses can reach the game before capture starts.
- Controller/game compatibility beyond the setup above needs testing.
- Automatic startup and Sunshine session hooks are not part of the tested installation path.
- The external keyboard is about 816 logical pixels wide and can clip on smaller displays. Its monitor placement follows the shell.
- Actual controller vibration and all monitor/phone combinations have not been individually verified.
- Restarting the bridge creates new virtual devices. Games, especially isolated sessions, may need restarting to discover them.

## Reproduce automated checks

Run from the checkout with python-evdev installed:

```sh
python -m unittest discover -s tests -v
```

The UI checks and preview renderer additionally need PySide6:

```sh
python scripts/ui_smoke.py
python scripts/render_preview.py /tmp/hyprcontroller-preview.png --width 640 --height 360
```

These use sample data without accessing controllers or changing workspaces. The README image was rendered from the implemented picker with Steam and Omikuji entries from workspace 2; it is not a full-desktop screenshot.

## Live checks

Stop any running hyprcontroller instance first. Close Steam and games before virtual-controller smoke tests because temporary gamepads can send input to host applications:

```sh
python scripts/bridge_smoke.py
python scripts/live_smoke.py
python scripts/keyboard_smoke.py
```

The gameplay scripts refuse to run while Steam is open. The bridge probe grabs its virtual output and captures generated mouse events rather than operating the desktop. The keyboard probe uses a disposable text window, the existing end4-pC US keyboard, PySide6, and grim; its coordinates assume a 1920×1080 host at scale 1. It restores pointer position and window focus and removes its temporary devices and window.

The bridge probe accepts `--runtime /path/to/SteamLinuxRuntime_4/run` for a nested-runtime device-view check. Its synthetic force-feedback source does not establish that physical vibration works.

### Real-controller trial

1. Start `hyprcontroller run --bridge` before the game. Connect the local or Moonlight controller and check `status`.
2. Open a game menu, hold L3 + R3, and release everything. The opening presses may affect the game; navigation after the picker opens should not.
3. Switch workspaces with LB/RB, including an empty workspace. Hold each bumper for several seconds: each press should switch once. Workspaces assigned to another monitor should stay there.
4. Select a window with A or hide the picker with B. The corner badge should remain, and game input should stay captured.
5. Move the left stick in circles for 20 seconds. Check for periodic stutter, then try clicking, held-A dragging, and right-stick scrolling.
6. Focus a disposable text field, press Y, and test typing, Shift, Backspace, Enter, and closing the keyboard.
7. Press Start to reopen the picker. Return to the game workspace; selecting the game should still keep desktop mode active.
8. Hold L3 + R3 again and release all controls. Verify gameplay resumes without stuck inputs and test physical rumble.
9. Disconnect in desktop mode, reconnect, wait for discovery, and reopen the picker. Repeat locally and through Moonlight.
10. Repeat with fullscreen games and your normal displays/phone resolution. Check input isolation in both picker and mouse views.

Use `resume` to request gameplay after neutral controls or `stop` for emergency shutdown. If a game reads original devices, exit it and Steam, start the bridge, connect the required controllers, launch through `hyprcontroller game -- steam`, and repeat the trial. An isolated session must restart to expose a newly created virtual controller.

When reporting a problem, include the game, controller model, local/remote connection, host versions, whether the isolated launcher was used, and the failed step.

## Backup and rollback

The installation manifest at `~/.local/state/hyprcontroller/install.json` records owned files and checksums. Backups stay outside the checkout in `~/.local/state/hyprcontroller/backups/`; the desktop archive can contain Sunshine pairing credentials and must remain private.

Installation verifies the initial archive by scratch extraction. Updates save and verify the installed version, preserve user configuration, and refuse later edits. Rollback removes unchanged owned files; `--keep-modified` retains edited ones. Installation does not modify the existing desktop/streaming configuration, enable startup, or add input-access rules.

```sh
python scripts/backup.py --verify ~/.local/state/hyprcontroller/backups/TIMESTAMP
python scripts/install.py rollback --dry-run
```

The installation tests cover older configuration preservation, corrupt archives, symlinks, modes, update backups, and rollback protection against later edits.
