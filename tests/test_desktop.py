import unittest
from unittest.mock import patch
from hyprcontroller.desktop import Desktop, DesktopError, workspace_list, query_snapshot
import json


class DesktopTests(unittest.TestCase):
    def test_workspaces_do_not_steal_other_monitor_or_special(self):
        items = [{'id': 2, 'name': '2', 'monitor': 'DP-3', 'monitorID': 0},
                 {'id': 9, 'name': '9', 'monitor': 'HDMI-A-1', 'monitorID': 1},
                 {'id': 11, 'name': 'work', 'monitor': 'DP-3', 'monitorID': 0},
                 {'id': -99, 'name': 'special:special', 'monitor': 'DP-3', 'monitorID': 0}]
        result = workspace_list(items, 'DP-3', 0, {'id': 2, 'name': '2'})
        names = [w['name'] for w in result]
        self.assertIn('1', names); self.assertIn('work', names)
        self.assertNotIn('9', names); self.assertNotIn('special:special', names)

    @patch('hyprcontroller.desktop.query_snapshot')
    def test_empty_workspace_is_valid(self, query):
        query.return_value = [[{'id': 0, 'name': 'DP-3', 'focused': True, 'activeWorkspace': {'id': 2, 'name': '2'}}], [], [], {}]
        data = Desktop().refresh()
        self.assertEqual(data['windows'], [])
        self.assertEqual(len(data['workspaces']), 10)

    @patch('hyprcontroller.desktop.query_snapshot')
    def test_configured_monitor_missing_is_an_error(self, query):
        query.return_value = [[{'id': 0, 'name': 'DP-3', 'focused': True}], [], [], {}]
        with self.assertRaises(DesktopError):
            Desktop().refresh('missing')

    @patch('hyprcontroller.desktop.hyprctl')
    def test_disappeared_window_is_not_focused(self, command):
        desktop = Desktop()
        with patch.object(desktop, 'refresh', return_value={'windows': []}):
            desktop.data = {'windows': []}
            with self.assertRaises(DesktopError):
                desktop.focus('0x123')
        command.assert_not_called()

    @patch('hyprcontroller.desktop.hyprctl')
    def test_batch_reply_reads_consecutive_json_values(self, command):
        values = [[{'name': 'DP-3'}], [], [{'title': 'A; B'}], {}]
        command.return_value = '\n'.join(json.dumps(v) for v in values)
        self.assertEqual(query_snapshot(), values)
        command.assert_called_once_with('-j', '--batch', 'monitors;workspaces;clients;activewindow')

    @patch('hyprcontroller.desktop.hyprctl')
    def test_incomplete_or_failed_batch_keeps_the_previous_snapshot(self, command):
        desktop = Desktop()
        desktop.data = {'windows': [{'address': '0x123'}]}
        before = desktop.data
        for reply in ('[] [] []', '[] [] [] error', '[] [] {} {}'):
            command.return_value = reply
            with self.assertRaises(DesktopError):
                desktop.refresh()
            self.assertIs(desktop.data, before)

    @patch('hyprcontroller.desktop.hyprctl')
    def test_invalid_window_address_never_reaches_lua(self, command):
        with self.assertRaises(DesktopError):
            Desktop().focus('0x1\"); os.execute("bad")')
        command.assert_not_called()
