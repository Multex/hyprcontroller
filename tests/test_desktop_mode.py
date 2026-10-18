import os
import tempfile
import unittest
from unittest.mock import Mock, patch
from evdev import AbsInfo, InputEvent, UInputError, ecodes as e
from hyprcontroller.config import Config
from hyprcontroller.input import ModeController, PadState
from hyprcontroller.backend import Service
from hyprcontroller.bridge import identity
from hyprcontroller.desktop import DesktopError
from hyprcontroller.pointer import DesktopPointer
from hyprcontroller.keyboard import Keyboard, KeyboardError


class PersistentInputTests(unittest.TestCase):
    def setUp(self):
        self.machine = ModeController(Config(input_mode='bridge', hold_seconds=.75))
        self.state = PadState()
        self.machine.request_open('local')
        self.machine.opened()

    def tick(self, now):
        return self.machine.tick('local', self.state, now)

    def test_select_waits_for_release_and_retains_desktop_ownership(self):
        self.state.buttons = {e.BTN_SOUTH}
        self.assertEqual(self.tick(1), [('view_pending', 'confirm')])
        self.assertEqual(self.machine.mode, 'desktop')
        self.assertEqual(self.tick(2), [])
        self.state.buttons.clear()
        self.assertEqual(self.tick(2.1), [('view', 'confirm')])
        self.machine.viewed('pointer')
        self.assertEqual(self.machine.owner, 'local')
        self.assertEqual(self.tick(2.2), [])

    def test_b_hides_switcher_without_resuming_game(self):
        self.state.buttons = {e.BTN_EAST}
        self.assertEqual(self.tick(1), [('view_pending', 'hide')])
        self.state.buttons.clear()
        self.assertEqual(self.tick(1.1), [('view', 'hide')])
        self.machine.viewed('pointer')
        self.assertEqual(self.machine.mode, 'desktop')

    def test_start_reopens_switcher_and_pointer_clicks_do_not_exit(self):
        self.machine.viewed('pointer')
        self.state.buttons = {e.BTN_SOUTH, e.BTN_EAST}
        self.assertEqual(self.tick(1), [])
        self.state.buttons = {e.BTN_START}
        self.assertEqual(self.tick(2), [('view_pending', 'switcher')])
        self.assertEqual(self.tick(3), [])
        self.state.buttons.clear()
        self.assertEqual(self.tick(3.1), [('view', 'switcher')])

    def test_gesture_resumes_from_either_view_only_after_neutral(self):
        for view in ('switcher', 'pointer'):
            with self.subTest(view=view):
                self.machine.reset(); self.machine.request_open('local'); self.machine.opened()
                self.machine.viewed(view)
                self.state.buttons = {e.BTN_THUMBL, e.BTN_THUMBR}
                self.assertEqual(self.tick(1), [])
                self.assertEqual(self.tick(1.76), [('closing', None)])
                self.assertEqual(self.tick(2), [])
                self.state.buttons.clear()
                self.assertEqual(self.tick(2.1), [('close', 'resume')])
                self.assertEqual(self.machine.mode, 'idle')

    def test_workspace_and_keyboard_controls_in_pointer_view(self):
        self.machine.viewed('pointer')
        self.state.buttons = {e.BTN_TR}
        self.assertEqual(self.tick(1), [('workspace', 1)])
        self.state.buttons.clear(); self.tick(1.1)
        self.state.buttons = {e.BTN_NORTH}
        self.assertEqual(self.tick(2), [('view_pending', 'keyboard_toggle')])
        self.state.buttons.clear()
        self.assertEqual(self.tick(2.1), [('view', 'keyboard_toggle')])

    def test_other_controller_cannot_exit_desktop_mode(self):
        other = PadState(buttons={e.BTN_THUMBL, e.BTN_THUMBR, e.BTN_START})
        self.assertEqual(self.machine.tick('remote', other, 10), [])
        self.assertEqual(self.machine.tick('remote', other, 12), [])
        self.assertEqual(self.machine.mode, 'desktop')

    def test_held_bumper_waits_for_release_after_view_or_input_state_reset(self):
        for view in ('switcher', 'pointer'):
            for button, delta in ((e.BTN_TL, -1), (e.BTN_TR, 1)):
                with self.subTest(view=view, button=button):
                    self.machine.reset()
                    self.machine.request_open('local')
                    self.machine.opened()
                    self.machine.viewed(view)
                    self.state.buttons = {button}
                    self.assertEqual(self.tick(1), [('workspace', delta)])
                    self.machine.previous.clear()
                    self.assertEqual(self.tick(2.5), [])
                    self.machine.viewed(view)
                    self.assertEqual(self.tick(4), [])
                    self.state.buttons.clear()
                    self.assertEqual(self.tick(4.1), [])
                    self.state.buttons = {button}
                    self.assertEqual(self.tick(4.2), [('workspace', delta)])


class PointerTests(unittest.TestCase):
    def setUp(self):
        self.device = Mock()
        self.pointer = DesktopPointer(Config(), self.device)
        self.state = PadState(ranges={code: AbsInfo(0, -32768, 32767, 0, 0, 0)
                                     for code in (e.ABS_X, e.ABS_Y, e.ABS_RX, e.ABS_RY)})

    def test_dead_zone_does_not_emit_motion(self):
        self.state.axes[e.ABS_X] = 5000
        self.pointer.update(self.state, 0); self.pointer.update(self.state, .1)
        self.device.write.assert_not_called()

    def test_fractional_motion_accumulates_and_large_time_gap_is_capped(self):
        self.state.axes[e.ABS_X] = 10000
        for tick in range(100):
            self.pointer.update(self.state, tick / 100)
        motions = [c.args[2] for c in self.device.write.call_args_list if c.args[:2] == (e.EV_REL, e.REL_X)]
        self.assertGreater(sum(motions), 0)
        self.state.axes[e.ABS_X] = 32767
        self.device.reset_mock()
        self.pointer.update(self.state, 100)
        self.device.write.assert_called_with(e.EV_REL, e.REL_X, 45)

    def test_click_drag_and_release(self):
        self.state.buttons = {e.BTN_SOUTH, e.BTN_EAST}
        self.pointer.update(self.state, 0)
        self.device.write.assert_any_call(e.EV_KEY, e.BTN_LEFT, 1)
        self.device.write.assert_any_call(e.EV_KEY, e.BTN_RIGHT, 1)
        self.device.reset_mock()
        self.pointer.update(self.state, .02)
        self.device.write.assert_not_called()
        self.pointer.release()
        self.device.write.assert_any_call(e.EV_KEY, e.BTN_LEFT, 0)
        self.device.write.assert_any_call(e.EV_KEY, e.BTN_RIGHT, 0)
        self.assertIsNone(self.pointer.last_tick)

    def test_right_stick_scrolls_in_expected_directions(self):
        self.state.axes.update({e.ABS_RX:32767, e.ABS_RY:32767})
        for tick in range(30):
            self.pointer.update(self.state, tick / 100)
        self.device.write.assert_any_call(e.EV_REL, e.REL_HWHEEL, 1)
        self.device.write.assert_any_call(e.EV_REL, e.REL_WHEEL, -1)


class PersistentBackendTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {'XDG_RUNTIME_DIR': self.tmp.name})
        self.env.start()
        self.service = Service(Config(input_mode='bridge'))
        self.service.emit = Mock()
        self.service.desktop = Mock()
        self.service.desktop.data = {'windows':[{'address':'0x123'}]}
        self.service.keyboard = Mock(touched=False)
        self.pad = Mock()
        self.pad.device.info = (3, 1118, 654, 1)
        self.pad.device.name = 'pad'
        self.pad.device.uniq = ''; self.pad.device.phys = 'usb/pad'
        self.pad.state = PadState()
        self.service.pads['local'] = self.pad
        self.bridge = Mock()
        self.bridge.key = identity(self.pad.device)
        self.bridge.path = '/dev/input/event99'
        self.bridge.nodes = [self.bridge.path]
        self.service.bridges.append(self.bridge)
        self.pad.bridge = self.bridge
        self.service.ui_ready = True
        self.service.machine.request_open('local')
        self.pointer_patch = patch('hyprcontroller.backend.DesktopPointer', return_value=Mock())
        self.pointer_factory = self.pointer_patch.start()
        self.addCleanup(self.pointer_patch.stop)
        self.service.open()

    def tearDown(self):
        self.service.release(); self.service.selector.close()
        self.env.stop(); self.tmp.cleanup()

    def test_select_focuses_without_resuming_bridge_or_injecting_click(self):
        self.service.begin_close(True)
        self.bridge.resume.assert_not_called()
        self.service.desktop.focus.assert_not_called()
        self.service.commit_view('confirm')
        pointer = self.service.pointer
        self.service.desktop.focus.assert_called_once_with('0x123')
        self.assertEqual(self.service.machine.view, 'pointer')
        self.assertEqual(self.service.machine.mode, 'desktop')
        self.assertTrue(self.service.pointer_active())
        self.bridge.resume.assert_not_called()
        pointer.update.assert_not_called()

    def test_keyboard_highlight_changes_selection_without_releasing_capture(self):
        self.service.desktop.data['windows'].append({'address': '0x456'})
        self.service.command({'command': 'highlight', 'index': 1})
        self.assertEqual(self.service.selected, 1)
        self.assertEqual(self.service.machine.view, 'switcher')
        self.assertEqual(self.service.machine.mode, 'desktop')
        self.assertIsNone(self.service.machine.pending_view)
        self.service.desktop.focus.assert_not_called()
        self.bridge.resume.assert_not_called()

    def test_invalid_keyboard_highlight_and_pointer_view_do_not_change_selection(self):
        self.service.command({'command': 'highlight', 'index': 99})
        self.assertEqual(self.service.selected, 0)
        self.service.machine.viewed('pointer')
        self.service.desktop.data['windows'].append({'address': '0x456'})
        self.service.command({'command': 'highlight', 'index': 1})
        self.assertEqual(self.service.selected, 0)

    def test_disappearing_window_retains_switcher_and_capture(self):
        self.service.begin_close(True)
        self.service.desktop.focus.side_effect = DesktopError('gone')
        self.service.commit_view('confirm')
        self.assertEqual(self.service.machine.mode, 'desktop')
        self.assertEqual(self.service.machine.view, 'switcher')
        self.assertIsNone(self.service.machine.pending_view)
        self.bridge.resume.assert_not_called()

    def test_toggle_hides_and_reopens_switcher_without_resuming(self):
        self.service.command({'command':'toggle'})
        self.service.commit_view('hide')
        self.service.command({'command':'toggle'})
        self.service.commit_view('switcher')
        self.assertEqual(self.service.machine.view, 'switcher')
        self.service.keyboard.close.assert_called_once()
        self.bridge.resume.assert_not_called()

    def test_resume_waits_for_neutral_then_cleans_pointer_and_keyboard(self):
        self.service.commit_view('hide')
        pointer=self.service.pointer
        self.pad.state.buttons={e.BTN_SOUTH}
        self.service.command({'command':'resume'})
        self.assertEqual(self.service.machine.mode, 'closing')
        self.assertEqual(self.service.machine.tick('local',self.pad.state,1), [])
        self.bridge.resume.assert_not_called()
        self.pad.state.buttons.clear()
        for action,value in self.service.machine.tick('local',self.pad.state,2):
            self.service.action(action,value)
        pointer.close.assert_called_once()
        self.service.keyboard.close.assert_called_once()
        self.bridge.resume.assert_called_once()
        self.assertEqual(self.service.machine.mode, 'idle')

    def test_open_does_not_wait_for_virtual_mouse_creation(self):
        self.pointer_factory.assert_not_called()
        self.assertIsNone(self.service.pointer)
        self.assertEqual(self.service.machine.view, 'switcher')

    def test_pointer_creation_failure_keeps_switcher_and_capture(self):
        self.pointer_factory.side_effect = UInputError('mouse unavailable')
        self.service.commit_view('hide')
        self.assertEqual(self.service.machine.view, 'switcher')
        self.assertEqual(self.service.machine.mode, 'desktop')
        self.assertIsNone(self.service.machine.pending_view)
        self.assertEqual(self.service.error, 'mouse unavailable')
        self.bridge.resume.assert_not_called()

    def test_keyboard_failure_keeps_desktop_capture(self):
        self.service.begin_view('keyboard')
        self.service.keyboard.open.side_effect = KeyboardError('unavailable')
        self.service.commit_view('keyboard')
        self.assertEqual(self.service.machine.mode, 'desktop')
        self.assertEqual(self.service.machine.view, 'pointer')
        self.assertEqual(self.service.error, 'unavailable')
        self.bridge.resume.assert_not_called()

    def test_batched_start_press_and_release_are_not_lost(self):
        self.service.machine.viewed('pointer')
        self.pad.device.read.return_value = [InputEvent(0,0,e.EV_KEY,e.BTN_START,1),
                                            InputEvent(0,0,e.EV_SYN,e.SYN_REPORT,0),
                                            InputEvent(0,0,e.EV_KEY,e.BTN_START,0),
                                            InputEvent(0,0,e.EV_SYN,e.SYN_REPORT,0)]
        self.service.dispatch_io('pad','local',None)
        self.assertEqual(self.service.machine.view,'switcher')
        self.bridge.resume.assert_not_called()


class KeyboardTests(unittest.TestCase):
    def test_calls_existing_shell_and_closes_only_after_use(self):
        keyboard=Keyboard(Config())
        with patch('hyprcontroller.keyboard.subprocess.run', return_value=Mock(returncode=0)) as run:
            keyboard.close(); run.assert_not_called()
            keyboard.open(); self.assertTrue(keyboard.touched)
            self.assertEqual(run.call_args.args[0],['qs','ipc','-c','end4-pC','call','osk','open'])
            keyboard.close(); self.assertFalse(keyboard.touched)

    def test_configuration_rejects_invalid_pointer_settings(self):
        with tempfile.NamedTemporaryFile(mode='w',suffix='.json') as f:
            f.write('{"pointer_speed": -1}'); f.flush()
            with self.assertRaises(ValueError): Config.load(f.name)
