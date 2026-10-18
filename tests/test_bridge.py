import errno
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from evdev import AbsInfo, InputEvent, ff, ecodes as e
from hyprcontroller.bridge import Bridge
from hyprcontroller.game import build_command


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.bridge = Bridge.__new__(Bridge)
        self.bridge.ui = Mock()
        self.bridge.source = Mock()
        self.bridge.paused = False
        self.bridge.effects = {}
        self.bridge.effect_templates = {}
        self.bridge.buttons = set()
        self.bridge.axes = {
            e.ABS_X: AbsInfo(10000, -32768, 32767, 0, 0, 0),
            e.ABS_Z: AbsInfo(100, 0, 255, 0, 0, 0),
            e.ABS_HAT0Y: AbsInfo(1, -1, 1, 0, 0, 0)}

    def event(self, kind, code, value):
        return InputEvent(0, 0, kind, code, value)

    def test_pause_releases_buttons_and_centers_sticks_and_triggers(self):
        b = self.bridge
        b.forward(self.event(e.EV_KEY, e.BTN_SOUTH, 1))
        b.effects[0] = 12
        b.pause()
        b.ui.write.assert_any_call(e.EV_KEY, e.BTN_SOUTH, 0)
        b.ui.write.assert_any_call(e.EV_ABS, e.ABS_X, 0)
        b.ui.write.assert_any_call(e.EV_ABS, e.ABS_Z, 0)
        b.ui.write.assert_any_call(e.EV_ABS, e.ABS_HAT0Y, 0)
        b.source.write.assert_called_with(e.EV_FF, 12, 0)
        self.assertFalse(b.buttons)

    def test_navigation_is_discarded_and_not_replayed_after_resume(self):
        b = self.bridge
        b.pause()
        b.ui.write_event.reset_mock()
        for event in (self.event(e.EV_ABS, e.ABS_X, 20000),
                      self.event(e.EV_KEY, e.BTN_SOUTH, 1),
                      self.event(e.EV_KEY, e.BTN_SOUTH, 0)):
            b.forward(event)
        b.resume()
        b.ui.write_event.assert_not_called()
        event = self.event(e.EV_KEY, e.BTN_SOUTH, 1)
        b.forward(event)
        b.ui.write_event.assert_called_once_with(event)

    def upload(self, virtual_id=3):
        effect = ff.Effect(e.FF_RUMBLE, virtual_id, 0, ff.Trigger(0, 0), ff.Replay(100, 0),
                           ff.EffectType(ff_rumble_effect=ff.Rumble(1000, 1000)))
        upload = SimpleNamespace(effect=effect, retval=None)
        self.bridge.ui.begin_upload.return_value = upload
        return upload

    def test_rumble_maps_ids_without_changing_games_effect_id(self):
        b = self.bridge
        upload = self.upload()
        b.source.upload_effect.return_value = 17
        b.feedback(self.event(e.EV_UINPUT, e.UI_FF_UPLOAD, 1))
        self.assertEqual(upload.effect.id, 3)
        self.assertEqual(upload.retval, 0)
        b.ui.end_upload.assert_called_once_with(upload)
        b.feedback(self.event(e.EV_FF, 3, 1))
        b.source.write.assert_called_with(e.EV_FF, 17, 1)
        b.paused = True
        b.feedback(self.event(e.EV_FF, 3, 1))
        b.source.write.assert_called_with(e.EV_FF, 17, 0)

    def test_failed_feedback_upload_always_completes_handshake(self):
        upload = self.upload()
        self.bridge.source.upload_effect.side_effect = OSError(errno.EIO, 'failed')
        self.bridge.feedback(self.event(e.EV_UINPUT, e.UI_FF_UPLOAD, 1))
        self.assertEqual(upload.retval, -errno.EIO)
        self.assertEqual(upload.effect.id, 3)
        self.bridge.ui.end_upload.assert_called_once_with(upload)

    def test_disconnect_keeps_virtual_device_and_restores_rumble_after_reconnect(self):
        b = self.bridge
        self.upload()
        b.source.upload_effect.return_value = 17
        b.feedback(self.event(e.EV_UINPUT, e.UI_FF_UPLOAD, 1))
        source = b.source
        b.detach()
        self.assertIsNone(b.source)
        source.ungrab.assert_called_once()
        b.ui.close.assert_not_called()
        replacement = Mock()
        replacement.upload_effect.return_value = 4
        b.attach(replacement)
        b.feedback(self.event(e.EV_FF, 3, 1))
        replacement.write.assert_called_with(e.EV_FF, 4, 1)

    def test_connected_source_cannot_be_replaced_or_grab_another_pad(self):
        source = self.bridge.source
        replacement = Mock()
        with self.assertRaises(OSError) as caught:
            self.bridge.attach(replacement)
        self.assertEqual(caught.exception.errno, errno.EBUSY)
        replacement.grab.assert_not_called()
        self.assertIs(self.bridge.source, source)


class GameViewTests(unittest.TestCase):
    def test_refuses_unprotected_mode(self):
        with patch('hyprcontroller.game.shutil.which', return_value='/usr/bin/bwrap'):
            with self.assertRaisesRegex(RuntimeError, 'run --bridge'):
                build_command(['/usr/bin/true'], {'input_mode': 'observe'})

    def test_device_view_contains_only_validated_virtual_inputs(self):
        status = {'input_mode': 'bridge', 'virtual_controllers': [
            {'path': '/dev/input/event9', 'nodes': ['/dev/input/event9', '/dev/input/js1']}]}
        device = Mock(name='device')
        device.name = 'hyprcontroller: Pad'
        device.phys = 'hyprcontroller/bridge'
        with patch('hyprcontroller.game.shutil.which', return_value='/usr/bin/bwrap'), \
             patch('hyprcontroller.game.InputDevice', return_value=device), \
             patch('hyprcontroller.game.Path.exists', return_value=True):
            args = build_command(['/usr/bin/true'], status)
        self.assertIn('--dev', args)
        self.assertIn('--unshare-pid', args)
        self.assertIn('/dev/input/event9', args)
        self.assertNotIn('/dev/uinput', args)
        self.assertNotIn('/dev/bus/usb', args)
        self.assertFalse(any('hidraw' in s for s in args))

    def test_refuses_to_reuse_existing_steam_process_outside_view(self):
        status = {'input_mode': 'bridge', 'virtual_controllers': [{}]}
        with patch('hyprcontroller.game.shutil.which', return_value='/usr/bin/bwrap'), \
             patch('hyprcontroller.game.steam_running', return_value=True):
            with self.assertRaisesRegex(RuntimeError, 'Exit your games and Steam'):
                build_command(['steam'], status)
