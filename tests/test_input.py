import unittest
from evdev import AbsInfo, InputEvent, ecodes as e
from hyprcontroller.config import Config
from hyprcontroller.input import ModeController, PadState


def event(kind, code, value):
    return InputEvent(0, 0, kind, code, value)


class RecordedInputTests(unittest.TestCase):
    def setUp(self):
        self.machine = ModeController(Config())
        self.pad = PadState(ranges={e.ABS_X: AbsInfo(0, -32768, 32767, 0, 128, 0),
                                   e.ABS_Y: AbsInfo(0, -32768, 32767, 0, 128, 0),
                                   e.ABS_Z: AbsInfo(0, 0, 255, 0, 0, 0)})

    def tick(self, at):
        return self.machine.tick('local', self.pad, at)

    def press(self, code):
        self.pad.update(event(e.EV_KEY, code, 1))

    def release(self, code):
        self.pad.update(event(e.EV_KEY, code, 0))

    def open(self):
        self.press(e.BTN_THUMBL); self.press(e.BTN_THUMBR)
        self.tick(0)
        self.assertEqual(self.tick(1), [('armed', None)])
        self.release(e.BTN_THUMBL); self.release(e.BTN_THUMBR)
        self.assertEqual(self.tick(1.1), [('open', None)])
        self.machine.opened()

    def test_observed_controller_hold_with_shorter_threshold(self):
        self.machine = ModeController(Config(hold_seconds=0.75))
        self.press(e.BTN_THUMBL); self.press(e.BTN_THUMBR)
        self.tick(3.94)
        self.assertEqual(self.tick(4.70), [('armed', None)])
        self.assertEqual(self.machine.mode, 'arming')
        self.release(e.BTN_THUMBL); self.release(e.BTN_THUMBR)
        self.assertEqual(self.tick(4.86), [('open', None)])

    def test_short_gesture_does_not_activate(self):
        self.press(e.BTN_THUMBL); self.press(e.BTN_THUMBR)
        self.tick(0); self.assertEqual(self.tick(.99), [])
        self.release(e.BTN_THUMBR)
        self.assertEqual(self.tick(1.5), [])
        self.assertEqual(self.machine.mode, 'idle')

    def test_activation_requires_full_release_and_neutral_axes(self):
        self.press(e.BTN_THUMBL); self.press(e.BTN_THUMBR)
        self.tick(0); self.tick(1)
        self.release(e.BTN_THUMBL)
        self.assertEqual(self.tick(1.1), [])
        self.release(e.BTN_THUMBR)
        self.pad.update(event(e.EV_ABS, e.ABS_X, 25000))
        self.assertEqual(self.tick(1.2), [])
        self.pad.update(event(e.EV_ABS, e.ABS_X, 700))
        self.assertEqual(self.tick(1.3), [('open', None)])

    def test_trigger_must_be_released(self):
        self.machine.request_open('local')
        self.pad.update(event(e.EV_ABS, e.ABS_Z, 255))
        self.assertEqual(self.tick(0), [])
        self.pad.update(event(e.EV_ABS, e.ABS_Z, 0))
        self.assertEqual(self.tick(.1), [('open', None)])

    def test_stick_dead_zone_and_timed_repeat(self):
        self.open()
        self.pad.update(event(e.EV_ABS, e.ABS_X, 5000))
        self.assertEqual(self.tick(2), [])
        self.pad.update(event(e.EV_ABS, e.ABS_X, 25000))
        self.assertEqual(self.tick(2.1), [('navigate', 'right')])
        self.assertEqual(self.tick(2.4), [])
        self.assertEqual(self.tick(2.46), [('navigate', 'right')])
        self.assertEqual(self.tick(2.5), [])
        self.assertEqual(self.tick(2.6), [('navigate', 'right')])

    def test_shoulder_press_switches_once_while_held(self):
        self.open(); self.press(e.BTN_TR)
        self.assertEqual(self.tick(2), [('workspace', 1)])
        self.assertEqual(self.tick(3), [])
        self.release(e.BTN_TR); self.tick(3.1); self.press(e.BTN_TR)
        self.assertEqual(self.tick(3.2), [('workspace', 1)])

    def test_shoulders_ignore_repeat_events_and_long_tick_gaps(self):
        for button, delta in ((e.BTN_TL, -1), (e.BTN_TR, 1)):
            with self.subTest(button=button):
                self.machine = ModeController(Config())
                self.pad.buttons.clear()
                self.open()
                self.press(button)
                self.assertEqual(self.tick(2), [('workspace', delta)])
                for at in (2.01, 3.5, 5, 10):
                    self.pad.update(event(e.EV_KEY, button, 2))
                    self.assertEqual(self.tick(at), [])
                self.release(button)
                self.assertEqual(self.tick(10.1), [])
                self.press(button)
                self.assertEqual(self.tick(10.2), [('workspace', delta)])

    def test_confirm_waits_for_neutral_before_returning_to_game(self):
        self.open(); self.press(e.BTN_SOUTH)
        self.assertEqual(self.tick(2), [('closing', None)])
        self.assertEqual(self.machine.mode, 'closing')
        self.assertEqual(self.tick(3), [])
        self.release(e.BTN_SOUTH)
        self.assertEqual(self.tick(3.1), [('close', 'confirm')])
        self.assertEqual(self.machine.mode, 'idle')

    def test_other_controller_cannot_operate_open_session(self):
        self.open()
        other = PadState(buttons={e.BTN_EAST})
        self.assertEqual(self.machine.tick('remote', other, 2), [])
        self.assertEqual(self.machine.mode, 'desktop')

    def test_disconnect_and_reconnect_start_idle(self):
        self.open()
        self.assertEqual(self.machine.remove('local'), [('disconnect', None)])
        self.assertEqual(self.machine.mode, 'idle')
        self.assertEqual(self.tick(5), [])

    def test_dpad_buttons_and_hat_navigation(self):
        self.open(); self.press(e.BTN_DPAD_UP)
        self.assertEqual(self.tick(2), [('navigate', 'up')])
        self.release(e.BTN_DPAD_UP); self.tick(2.1)
        self.pad.update(event(e.EV_ABS, e.ABS_HAT0Y, 1))
        self.assertEqual(self.tick(2.2), [('navigate', 'down')])


if __name__ == '__main__':
    unittest.main()
