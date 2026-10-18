#!/usr/bin/env python3
"""Check the actual QML view offscreen, without desktop or gamepad access."""
import argparse
import copy
import json
import os
from pathlib import Path

os.environ['QT_QPA_PLATFORM'] = 'offscreen'
os.environ['QT_QUICK_BACKEND'] = 'software'
from PySide6.QtCore import QPoint, QPointF, QUrl, Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtQuick import QQuickView
from PySide6.QtTest import QTest

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--output', type=Path, help='Optional directory for review screenshots')
args = parser.parse_args()
root = Path(__file__).resolve().parent.parent
app = QGuiApplication([])
view = QQuickView()
view.setResizeMode(QQuickView.SizeRootObjectToView)
view.setSource(QUrl.fromLocalFile(str(root/'ui/DesktopView.qml')))
if view.status() == QQuickView.Error:
    raise RuntimeError('\n'.join(error.toString() for error in view.errors()))
item = view.rootObject()
state = {}
actions = []


def receive(message):
    if hasattr(message, 'toVariant'):
        message = message.toVariant()
    actions.append(message)
    if message['command'] == 'highlight':
        state['selected'] = message['index']
    elif message['command'] == 'navigate':
        columns = item.property('columns')
        delta = {'left': -1, 'right': 1, 'up': -columns, 'down': columns}[message['direction']]
        state['selected'] = max(0, min(len(state['windows'])-1, state['selected']+delta))
    else:
        return
    item.setProperty('snapshot', copy.deepcopy(state))


item.action.connect(receive)


def show(width, height, empty=False, loading=False, long_name=False, count=4):
    state.clear()
    state.update(mode='desktop', view='switcher', input_mode='bridge', loading=loading,
                 controller='Controller '*20 if long_name else 'Xbox controller', monitor='DP-3',
                 selected=0, activation_hint='L3 + R3  ·  Hold 0.75s',
                 workspace={'id': 2, 'name': '2'},
                 workspaces=[{'id': n, 'name': str(n)} for n in range(1, 11)],
                 windows=[] if empty else [{'address': hex(n+1), 'title': f'Window {n+1}',
                                           'app': 'Test app', 'active': False} for n in range(count)])
    view.resize(width, height)
    item.setProperty('snapshot', copy.deepcopy(state))
    view.show()
    view.requestActivate()
    QTest.qWait(200)
    item.forceActiveFocus()
    actions.clear()


def key(code):
    actions.clear()
    QTest.keyClick(view, code)
    QTest.qWait(30)
    return list(actions)


def nodes(node):
    yield node
    for child in node.childItems():
        yield from nodes(child)


def assert_layout(node, scroll=False):
    visible = node.isVisible()
    pos = node.mapToItem(view.contentItem(), QPointF())
    if visible and (node.inherits('QQuickGridView') or node.inherits('QQuickListView')):
        assert pos.x() >= 0 and pos.y() >= 0
        assert pos.x()+node.width() <= view.width()+.5
        assert pos.y()+node.height() <= view.height()+.5
    if visible and node.inherits('QQuickText') and not scroll:
        assert pos.x() >= -.5 and pos.y() >= -.5, node.property('text')
        assert pos.x()+node.width() <= view.width()+.5, node.property('text')
        assert pos.y()+node.height() <= view.height()+.5, node.property('text')
        panel = item.findChild(type(item), 'overlayPanel')
        assert panel is not None
        if panel is not None:
            origin = panel.mapToItem(view.contentItem(), QPointF())
            assert pos.x() >= origin.x()-.5, node.property('text')
            assert pos.y() >= origin.y()-.5, node.property('text')
            assert pos.x()+node.width() <= origin.x()+panel.width()+.5, node.property('text')
            assert pos.y()+node.height() <= origin.y()+panel.height()+.5, node.property('text')
    scroll = scroll or node.inherits('QQuickFlickable')
    for child in node.childItems():
        assert_layout(child, scroll)


results = []
scenarios = [(640,360,False,False,False), (390,360,False,False,False),
             (320,568,False,False,False), (320,360,False,False,False),
             (320,568,True,False,False), (320,360,True,False,False),
             (640,360,True,False,False), (800,600,False,False,False),
             (1280,720,False,False,True), (320,360,True,True,False),
             (640,360,False,True,False)]
for width, height, empty, loading, long_name in scenarios:
    show(width, height, empty, loading, long_name)
    assert_layout(item)
    label = f'{width}x{height}' + ('-loading' if loading else '-empty' if empty else '-long-name' if long_name else '')
    if args.output:
        args.output.mkdir(parents=True, exist_ok=True)
        assert view.grabWindow().save(str(args.output/(label+'.png')))
    if loading:
        texts = [n.property('text') for n in nodes(item) if n.metaObject().className() == 'QQuickText' and n.isVisible()]
        if empty:
            assert 'Updating windows…' in texts
        else:
            assert 'Window 1' in texts
            assert 'Updating windows…' not in texts
        assert 'This workspace is empty' not in texts
        assert key(Qt.Key_Return) == []
        assert {'command': 'cancel'} in key(Qt.Key_Escape)
    results.append(label)

panel = next(n for n in nodes(item) if n.objectName() == 'overlayPanel')
show(1920, 1080)
assert panel.width() == 760 and panel.height() <= 560
position = panel.mapToItem(view.contentItem(), QPointF())
assert position.x() == 580 and abs(position.y() - (1080-panel.height())/2) <= .5
assert_layout(item)
if args.output:
    assert view.grabWindow().save(str(args.output/'1920x1080-centered.png'))
results.append('1920x1080-centered')

many_height = panel.height()
show(1920, 1080, count=1)
assert panel.height() < many_height
assert sum(n.property('text') == 'Window 1' for n in nodes(item) if n.inherits('QQuickText') and n.isVisible()) == 1
assert_layout(item)
if args.output:
    assert view.grabWindow().save(str(args.output/'1920x1080-one-window.png'))
results.append('1920x1080-one-window')

show(1920, 1080, empty=True)
assert panel.height() < many_height
assert_layout(item)
if args.output:
    assert view.grabWindow().save(str(args.output/'1920x1080-empty.png'))
results.append('1920x1080-empty')

show(800, 600)
assert {'command': 'confirm'} in key(Qt.Key_Return)
assert {'command': 'workspace', 'delta': -1} in key(Qt.Key_PageUp)
assert {'command': 'workspace', 'delta': 1} in key(Qt.Key_PageDown)
key(Qt.Key_Tab)
assert view.activeFocusItem().property('index') == 0
key(Qt.Key_Tab)
assert view.activeFocusItem().property('index') == 1
assert state['selected'] == 1
if args.output:
    assert view.grabWindow().save(str(args.output/'keyboard-focus.png'))
assert {'command': 'select', 'index': 1} in key(Qt.Key_Space)
assert {'command': 'select', 'index': 1} in key(Qt.Key_Return)
key(Qt.Key_Right)
assert state['selected'] == 2
assert view.activeFocusItem().property('index') == 2
key(Qt.Key_Backtab)
assert view.activeFocusItem().property('index') == 1
assert {'command': 'cancel'} in key(Qt.Key_Escape)
actions.clear()
first_card = next(n for n in nodes(item) if n.property('index') == 0 and n.property('activeFocusOnTab'))
click = first_card.mapToItem(view.contentItem(), QPointF(first_card.width()/2, first_card.height()/2))
QTest.mouseClick(view, Qt.LeftButton, Qt.NoModifier, click.toPoint())
assert {'command': 'select', 'index': 0} in actions
actions.clear()
QTest.mouseClick(view, Qt.LeftButton, Qt.NoModifier, QPoint(5,5))
assert {'command': 'cancel'} in actions

show(640,360,count=20)
for index in range(20):
    key(Qt.Key_Tab)
    assert view.activeFocusItem().property('index') == index, (index, view.activeFocusItem().metaObject().className())
for index in range(18,-1,-1):
    key(Qt.Key_Backtab)
    assert view.activeFocusItem().property('index') == index, (index, view.activeFocusItem().property('index'))
key(Qt.Key_Backtab)
assert view.activeFocusItem().property('index') == 19
key(Qt.Key_Tab)
assert view.activeFocusItem().property('index') == 0

print(json.dumps({'layout_scenarios': results, 'keyboard_mouse_and_loading': 'PASS', 'scrolling_tab_order': 'PASS'}, indent=2))
view.close()
