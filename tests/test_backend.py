import contextlib
import io
import json
import os
import tempfile
import unittest
from unittest.mock import Mock, patch
from hyprcontroller.backend import Service, compatible_gamepad
from hyprcontroller.config import Config
from hyprcontroller.desktop import DesktopError
from hyprcontroller.input import PadState
from evdev import AbsInfo, DeviceInfo, InputEvent, UInputError, ecodes as e


class BackendHandoffTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {'XDG_RUNTIME_DIR': self.tmp.name})
        self.env.start()
        self.service = Service(Config())
        self.service.emit = Mock()
        self.service.desktop = Mock()
        self.service.desktop.data = {'monitor': 'DP-3', 'windows': [{'address': '0x123'}]}
        self.pad = Mock()
        self.pad.state = PadState()
        self.pad.name = 'Test controller'
        self.service.pads['test'] = self.pad
        self.service.machine.request_open('test')
        self.service.ui_ready = True

    def tearDown(self):
        self.service.release()
        self.service.selector.close()
        self.env.stop(); self.tmp.cleanup()

    def test_open_requires_ui_ready_and_does_not_grab_on_failure(self):
        self.service.ui_ready = False
        self.service.open()
        self.pad.device.grab.assert_not_called()
        self.assertEqual(self.service.machine.mode, 'idle')

    def test_failed_grab_returns_idle(self):
        self.pad.device.grab.side_effect = OSError('busy')
        self.service.open()
        self.assertEqual(self.service.machine.mode, 'idle')
        self.assertIsNone(self.service.grabbed)

    def test_release_when_overlay_disconnects(self):
        self.service.open()
        self.assertEqual(self.service.machine.mode, 'desktop')
        for action, value in self.service.machine.remove('test'):
            self.service.action(action, value)
        self.pad.device.ungrab.assert_called_once()
        self.assertEqual(self.service.machine.mode, 'idle')

    def test_confirm_waits_then_focuses_the_captured_address(self):
        self.service.open()
        self.service.begin_close(True)
        self.service.desktop.focus.assert_not_called()
        self.assertEqual(self.service.machine.mode, 'closing')
        for action, value in self.service.machine.tick('test', self.pad.state, 2):
            self.service.action(action, value)
        self.pad.device.ungrab.assert_called_once()
        self.service.desktop.focus.assert_called_once_with('0x123')

    def test_monitor_failure_never_grabs_device(self):
        self.service.desktop.refresh.side_effect = DesktopError('monitor removed')
        self.service.open()
        self.pad.device.grab.assert_not_called()
        self.assertEqual(self.service.machine.mode, 'idle')

    def test_empty_workspace_confirm_leaves_desktop_open(self):
        self.service.open()
        self.service.desktop.data['windows'] = []
        self.service.machine.request_close('confirm')
        self.service.action('closing', None)
        self.assertEqual(self.service.machine.mode, 'desktop')
        self.pad.device.ungrab.assert_not_called()

    def test_repeated_error_actions_have_distinct_sequences_but_heartbeat_does_not(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.service.fail('Keyboard unavailable')
            first = self.service.state()
            heartbeat = self.service.command({'command': 'heartbeat'})
            self.service.fail('Keyboard unavailable')
            second = self.service.state()
        self.assertEqual(first['error'], second['error'])
        self.assertGreater(second['error_sequence'], first['error_sequence'])
        self.assertEqual(heartbeat['error_sequence'], first['error_sequence'])

    def test_refresh_announces_loading_before_query_and_clears_it_on_success(self):
        states = []
        self.service.emit = lambda: states.append(self.service.state()['loading'])

        def query(*args):
            self.assertTrue(self.service.loading)
            self.assertEqual(states, [True])

        self.service.desktop.refresh.side_effect = query
        self.service.refresh()
        self.assertEqual(states, [True, False])
        self.assertFalse(self.service.loading)

    def test_failed_refresh_clears_loading_and_keeps_the_last_snapshot(self):
        states = []
        self.service.emit = lambda: states.append(self.service.state()['loading'])
        before = self.service.desktop.data
        self.service.desktop.refresh.side_effect = DesktopError('query failed')
        with self.assertRaises(DesktopError):
            self.service.refresh()
        self.assertEqual(states, [True, False])
        self.assertFalse(self.service.loading)
        self.assertEqual(self.service.desktop.data, before)

    def test_open_uses_ready_event_cache_without_another_query(self):
        self.service.events = Mock()
        self.service.open()
        self.service.desktop.refresh.assert_not_called()
        self.assertEqual(self.service.machine.mode, 'desktop')

    def test_open_refreshes_if_a_pending_event_could_make_cache_stale(self):
        self.service.events = Mock()
        self.service.refresh_at = 1
        self.service.open()
        self.service.desktop.refresh.assert_called_once_with('')
        self.assertIsNone(self.service.refresh_at)

    def test_idle_refresh_tracks_the_active_or_configured_monitor(self):
        self.service.machine.reset()
        self.service.refresh()
        self.service.desktop.refresh.assert_called_once_with('')

    def test_window_events_refresh_the_idle_cache_without_a_debounce_delay(self):
        self.service.machine.reset()
        self.service.events = Mock()
        self.service.events.recv.return_value = b'windowtitlev2>>0x123,A new title\n'
        with patch('hyprcontroller.backend.time.monotonic', return_value=10):
            self.service.dispatch_io('events', None, None)
        self.assertEqual(self.service.refresh_at, 10)
        self.assertEqual(self.service.machine.mode, 'idle')

    def test_rumble_handshake_failure_keeps_session_alive_and_processes_later_requests(self):
        self.service.open()
        for code in (e.UI_FF_UPLOAD, e.UI_FF_ERASE):
            for phase in ('begin', 'end'):
                with self.subTest(code=code, phase=phase):
                    failed = InputEvent(0, 0, e.EV_UINPUT, code, 1)
                    following = InputEvent(0, 0, e.EV_UINPUT, code, 2)
                    bridge = Mock()
                    bridge.ui.read.return_value = [failed, following]
                    bridge.feedback.side_effect = [UInputError(f'Failed to {phase} uinput request'), None]
                    with contextlib.redirect_stderr(io.StringIO()):
                        self.service.dispatch_io('feedback', bridge, None)
                    self.assertEqual(bridge.feedback.call_count, 2)
                    self.assertEqual(bridge.feedback.call_args.args, (following,))
                    self.assertTrue(self.service.running)
                    self.assertEqual(self.service.machine.mode, 'desktop')
                    self.assertIs(self.service.grabbed, self.pad)
                    self.assertEqual(self.service.command({'command': 'status'})['mode'], 'desktop')

    def test_feedback_read_failure_does_not_stop_the_service(self):
        bridge = Mock()
        bridge.ui.read.side_effect = OSError('device disconnected')
        with contextlib.redirect_stderr(io.StringIO()):
            self.service.dispatch_io('feedback', bridge, None)
        self.assertTrue(self.service.running)
        self.assertIn('feedback read failed', self.service.error)


class StateOutputTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {'XDG_RUNTIME_DIR': self.tmp.name})
        self.env.start()
        self.service = Service(Config())
        self.read_end, self.write_end = os.pipe()
        os.set_blocking(self.read_end, False)
        os.set_blocking(self.write_end, False)
        self.stdout = patch('hyprcontroller.backend.sys.stdout', Mock(fileno=lambda: self.write_end))
        self.stdout.start()

    def tearDown(self):
        self.stdout.stop()
        for fd in (self.read_end, self.write_end):
            with contextlib.suppress(OSError):
                os.close(fd)
        self.service.selector.close()
        self.env.stop(); self.tmp.cleanup()

    def drain(self):
        received = b''
        while True:
            try:
                chunk = os.read(self.read_end, 65536)
            except BlockingIOError:
                self.service.flush_state()
                if not self.service.unsent_state and not self.service.newest_state:
                    with contextlib.suppress(BlockingIOError):
                        received += os.read(self.read_end, 65536)
                    return received
                continue
            received += chunk

    def test_full_pipe_does_not_block_and_ui_gets_only_complete_states_ending_with_the_newest(self):
        self.service.desktop.data = {'windows': [{'title': 'x' * 200000}]}
        for sequence in range(5):
            self.service.error_sequence = sequence
            self.service.emit()
        self.assertTrue(self.service.running)
        lines = self.drain().splitlines()
        states = [json.loads(line) for line in lines]
        self.assertEqual(states[0]['error_sequence'], 0)
        self.assertEqual(states[-1]['error_sequence'], 4)
        self.assertLess(len(states), 5)

    def test_closed_ui_pipe_stops_the_service(self):
        os.close(self.read_end)
        self.service.emit()
        self.assertFalse(self.service.running)


class DiscoveryTests(unittest.TestCase):
    def test_virtual_keyboard_mouse_is_not_a_gamepad(self):
        self.assertFalse(compatible_gamepad({e.EV_KEY: [e.BTN_SOUTH, e.BTN_THUMBL, e.BTN_THUMBR], e.EV_REL: [e.REL_X, e.REL_Y]}))

    def test_standard_local_or_remote_gamepad_is_accepted(self):
        axis = AbsInfo(0, -32768, 32767, 0, 0, 0)
        self.assertTrue(compatible_gamepad({e.EV_KEY: [e.BTN_SOUTH], e.EV_ABS: [(e.ABS_X, axis), (e.ABS_Y, axis)]}))


class DiscoveryCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {'XDG_RUNTIME_DIR': self.tmp.name})
        self.env.start()
        self.service = Service(Config())
        self.service.emit = Mock()
        self.path = '/dev/input/event1'
        self.paths = [self.path]
        self.stamp = (1, 2, 3, 4)
        self.device = Mock(name='input', fd=10)
        self.device.name = 'Mouse'
        self.device.capabilities.return_value = {e.EV_KEY: [e.BTN_LEFT]}
        self.glob = patch('hyprcontroller.backend.Path.glob', side_effect=lambda pattern: self.paths)
        self.stat = patch('hyprcontroller.backend.device_stamp', side_effect=lambda path: self.stamp)
        self.input = patch('hyprcontroller.backend.InputDevice', return_value=self.device)
        self.glob.start()
        self.stat.start()
        self.open_device = self.input.start()

    def tearDown(self):
        for pad in self.service.pads.values():
            pad.device.close()
        self.service.selector.close()
        self.input.stop()
        self.stat.stop()
        self.glob.stop()
        self.env.stop()
        self.tmp.cleanup()

    def make_gamepad(self):
        self.device.name = 'Gamepad'
        axis = AbsInfo(0, -32768, 32767, 0, 0, 0)
        self.device.capabilities.return_value = {
            e.EV_KEY: [e.BTN_SOUTH, e.BTN_THUMBL, e.BTN_THUMBR],
            e.EV_ABS: [(e.ABS_X, axis), (e.ABS_Y, axis)]}
        self.device.active_keys.return_value = []
        self.service.selector.close()
        self.service.selector = Mock()

    def test_unchanged_non_gamepads_are_not_reopened_on_periodic_scans(self):
        for _ in range(10):
            self.service.scan()
        self.open_device.assert_called_once_with(self.path)
        self.device.close.assert_called_once()
        self.assertEqual(self.service.pads, {})

    def test_metadata_change_rechecks_previously_rejected_device(self):
        self.service.scan()
        self.make_gamepad()
        self.stamp = (1, 2, 3, 5)
        self.service.scan()
        self.assertEqual(self.open_device.call_count, 2)
        self.assertIn(self.path, self.service.pads)

    def test_removed_then_reused_node_is_discovered_again(self):
        self.service.scan()
        self.paths.clear()
        self.service.scan()
        self.make_gamepad()
        self.paths.append(self.path)
        self.service.scan()
        self.assertIn(self.path, self.service.pads)

    def test_virtual_controller_outputs_are_not_reopened(self):
        self.make_gamepad()
        self.device.name = 'hyprcontroller: Gamepad'
        self.service.scan()
        self.service.scan()
        self.open_device.assert_called_once()
        self.assertEqual(self.service.pads, {})

    def test_cached_missing_button_error_is_still_reported(self):
        self.make_gamepad()
        self.device.capabilities.return_value[e.EV_KEY] = [e.BTN_SOUTH]
        self.service.scan()
        self.service.scan()
        self.open_device.assert_called_once()
        self.assertEqual(self.service.scan_errors, ['Gamepad: missing activation buttons'])

    def test_unreadable_device_is_retried(self):
        self.open_device.side_effect = [PermissionError(13, 'Access denied'), self.device]
        self.service.scan()
        self.make_gamepad()
        self.service.scan()
        self.assertIn(self.path, self.service.pads)


class DuplicateControllerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {'XDG_RUNTIME_DIR': self.tmp.name})
        self.env.start()
        self.service = Service(Config(input_mode='bridge'))
        self.service.selector.close()
        self.service.selector = Mock()
        self.service.emit = Mock()
        self.devices = {}
        for number in range(1, 4):
            path = f'/dev/input/event{number}'
            device = Mock(path=path, fd=number, uniq='', phys='virtual/pad')
            device.name = 'Identical pad'
            device.info = DeviceInfo(3, 1118, 654, 1)
            axis = AbsInfo(0, -32768, 32767, 0, 0, 0)
            device.absinfo.return_value = axis
            device.capabilities.return_value = {
                e.EV_KEY: [e.BTN_SOUTH, e.BTN_THUMBL, e.BTN_THUMBR],
                e.EV_ABS: [(e.ABS_X, axis), (e.ABS_Y, axis)]}
            device.active_keys.return_value = []
            self.devices[path] = device
        self.available = list(self.devices)[:2]
        self.virtuals = []

        def make_virtual(device, **kwargs):
            ui = Mock(fd=100 + len(self.virtuals))
            ui.device.path = f'/dev/input/event{ui.fd}'
            self.virtuals.append(ui)
            return ui

        self.glob = patch('hyprcontroller.backend.Path.glob',
                          side_effect=lambda pattern: self.available if pattern == 'event*' else [])
        self.input = patch('hyprcontroller.backend.InputDevice', side_effect=self.devices.__getitem__)
        self.uinput = patch('hyprcontroller.bridge.UInput.from_device', side_effect=make_virtual)
        self.glob.start(); self.input.start(); self.uinput.start()
        self.service.scan()

    def tearDown(self):
        self.service.release()
        for bridge in self.service.bridges:
            bridge.close()
        self.service.selector.close()
        self.uinput.stop(); self.input.stop(); self.glob.stop()
        self.env.stop(); self.tmp.cleanup()

    def test_identical_pads_have_separate_output_and_pause_only_owner(self):
        pads = list(self.service.pads.values())
        self.assertEqual(len(self.service.bridges), 2)
        self.assertIsNot(pads[0].bridge, pads[1].bridge)
        self.service.ui_ready = True
        self.service.desktop = Mock(data={'windows': []})
        self.service.machine.request_open(self.available[0])
        with patch('hyprcontroller.backend.DesktopPointer', return_value=Mock()):
            self.service.open()
        self.assertTrue(pads[0].bridge.paused)
        self.assertFalse(pads[1].bridge.paused)
        events = [InputEvent(0, 0, e.EV_KEY, e.BTN_SOUTH, 1),
                  InputEvent(0, 0, e.EV_SYN, e.SYN_REPORT, 0)]
        for path, pad in self.service.pads.items():
            pad.device.read.return_value = events
            pad.bridge.ui.write_event.reset_mock()
            self.service.dispatch_io('pad', path, None)
        pads[0].bridge.ui.write_event.assert_not_called()
        self.assertEqual(pads[1].bridge.ui.write_event.call_count, 2)
        self.assertEqual(len(self.service.state()['virtual_controllers']), 2)

    def test_disconnect_does_not_detach_identical_remaining_pad(self):
        first, second = [self.service.pads[path] for path in self.available]
        self.service.remove_pad(self.available[0])
        self.assertIsNone(first.bridge.source)
        self.assertIs(second.bridge.source, second.device)
        second.device.ungrab.assert_not_called()

    def test_reconnect_reuses_only_disconnected_virtual_controller(self):
        first, second = [self.service.pads[path] for path in self.available]
        self.service.remove_pad(self.available[0])
        self.available = list(self.devices)[1:]
        self.service.scan()
        replacement = self.service.pads[self.available[1]]
        self.assertIs(replacement.bridge, first.bridge)
        self.assertIs(second.bridge.source, second.device)
        self.assertIs(first.bridge.source, replacement.device)
        self.assertEqual(len(self.virtuals), 2)
