#!/usr/bin/env python3
"""Render the actual desktop view without accessing the person's display."""
import argparse
import json
import os
from pathlib import Path
import sys
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
os.environ['QT_QUICK_BACKEND'] = 'software'
from PySide6.QtCore import QTimer, QUrl
from PySide6.QtGui import QGuiApplication
from PySide6.QtQuick import QQuickView

parser = argparse.ArgumentParser()
parser.add_argument('output', type=Path)
parser.add_argument('--width', type=int, default=1280)
parser.add_argument('--height', type=int, default=720)
parser.add_argument('--empty', action='store_true')
parser.add_argument('--single', action='store_true', help='Show one sample window')
args = parser.parse_args()
app = QGuiApplication([])
view = QQuickView()
view.setResizeMode(QQuickView.SizeRootObjectToView)
view.resize(args.width, args.height)
view.setSource(QUrl.fromLocalFile(str(Path(__file__).resolve().parent.parent / 'ui/DesktopView.qml')))
if view.status() == QQuickView.Error:
    raise SystemExit(1)
state = {'mode': 'desktop', 'input_mode': 'bridge', 'activation_hint': 'L3 + R3  ·  Hold 0.75s',
         'controller': 'Xbox controller', 'monitor': 'DP-3',
         'workspace': {'id': 2, 'name': '2'},
         'workspaces': [{'id': n, 'name': str(n)} for n in range(1, 11)], 'selected': 0,
         'windows': [] if args.empty else [
             {'address': '0x1', 'title': 'Hollow Knight', 'app': 'Steam', 'active': True},
             {'address': '0x2', 'title': 'Music for the evening', 'app': 'Firefox', 'active': False},
             {'address': '0x3', 'title': 'Downloads', 'app': 'Files', 'active': False},
             {'address': '0x4', 'title': 'hyprcontroller', 'app': 'Terminal', 'active': False}]}
if args.single:
    state['windows'] = state['windows'][:1]
view.rootObject().setProperty('snapshot', state)
preview_icons = {
    'Steam': '/usr/share/icons/hicolor/256x256/apps/steam.png',
    'Firefox': '/usr/share/icons/breeze-plus/apps/48/firefox.svg',
    'Files': '/usr/share/icons/hicolor/scalable/apps/org.kde.dolphin.svg',
    'Terminal': '/usr/share/icons/breeze/apps/64/utilities-terminal.svg',
}
preview_icons = {name: QUrl.fromLocalFile(path).toString() for name, path in preview_icons.items() if Path(path).is_file()}
view.rootObject().setProperty('iconResolver', view.engine().evaluate('app => (' + json.dumps(preview_icons) + ')[app] || ""'))
view.resize(args.width, args.height)
view.show()

def capture():
    image = view.grabWindow()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if image.isNull() or not image.save(str(args.output)):
        print('Could not render preview', file=sys.stderr)
        app.exit(1)
    else:
        print(str(args.output.resolve()))
        app.quit()

QTimer.singleShot(500, capture)
raise SystemExit(app.exec())
