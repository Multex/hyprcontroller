"""Input normalization and mode transitions, independent of device I/O."""
from dataclasses import dataclass, field
from evdev import ecodes as e


BOUNCE_SECONDS = 0.04


@dataclass
class PadState:
    buttons: set = field(default_factory=set)
    axes: dict = field(default_factory=dict)
    ranges: dict = field(default_factory=dict)

    def update(self, event):
        if event.type == e.EV_KEY:
            if event.value:
                self.buttons.add(event.code)
            else:
                self.buttons.discard(event.code)
        elif event.type == e.EV_ABS:
            self.axes[event.code] = event.value

    def normalized(self, code):
        value = self.axes.get(code, 0)
        if code in (e.ABS_HAT0X, e.ABS_HAT0Y):
            return max(-1, min(1, value))
        bounds = self.ranges.get(code)
        if bounds is None or bounds.max == bounds.min:
            return 0
        if code in (e.ABS_Z, e.ABS_RZ, e.ABS_BRAKE, e.ABS_GAS):
            return max(0, min(1, (value - bounds.min) / (bounds.max - bounds.min)))
        center = (bounds.max + bounds.min) / 2
        return max(-1, min(1, (value - center) / ((bounds.max - bounds.min) / 2)))

    def neutral(self, dead_zone):
        codes = (e.ABS_X, e.ABS_Y, e.ABS_RX, e.ABS_RY, e.ABS_Z, e.ABS_RZ,
                 e.ABS_BRAKE, e.ABS_GAS, e.ABS_HAT0X, e.ABS_HAT0Y)
        return not self.buttons and all(abs(self.normalized(c)) <= dead_zone for c in codes)

    def direction(self, threshold):
        x, y = self.normalized(e.ABS_HAT0X), self.normalized(e.ABS_HAT0Y)
        if e.BTN_DPAD_LEFT in self.buttons:
            x = -1
        elif e.BTN_DPAD_RIGHT in self.buttons:
            x = 1
        if e.BTN_DPAD_UP in self.buttons:
            y = -1
        elif e.BTN_DPAD_DOWN in self.buttons:
            y = 1
        if not x and not y:
            x, y = self.normalized(e.ABS_X), self.normalized(e.ABS_Y)
        if max(abs(x), abs(y)) < threshold:
            return None
        if abs(x) >= abs(y):
            return 'right' if x > 0 else 'left'
        return 'down' if y > 0 else 'up'


class ModeController:
    def __init__(self, config):
        self.config = config
        self.mode = 'idle'
        self.owner = None
        self.holds = {}
        self.spent = set()
        self.previous = set()
        self.workspace_held = set()
        self.held_buttons = set()
        self.released_at = {}
        self.last_direction = None
        self.repeat_at = 0
        self.exit_action = 'cancel'
        self.view = 'switcher'
        self.pending_view = None
        self.persistent = config.input_mode == 'bridge'

    def request_open(self, owner):
        if self.mode == 'idle':
            self.owner = owner
            self.mode = 'arming'
            return True
        return False

    def request_close(self, action='cancel'):
        if self.mode in ('desktop', 'arming'):
            self.mode = 'closing'
            self.exit_action = action

    def opened(self):
        self.mode = 'desktop'
        self.view = 'switcher'
        self.pending_view = None
        self.holds.clear()
        self.previous.clear()
        self.workspace_held.clear()
        self.held_buttons.clear()
        self.released_at.clear()
        self.last_direction = None

    def request_view(self, action):
        if self.persistent and self.mode == 'desktop' and self.pending_view is None:
            self.pending_view = action
            return True
        return False

    def viewed(self, view):
        self.view = view
        self.pending_view = None
        self.previous.clear()
        self.last_direction = None

    def reset(self):
        self.mode = 'idle'
        self.owner = None
        self.holds.clear()
        self.spent.clear()
        self.previous.clear()
        self.workspace_held.clear()
        self.held_buttons.clear()
        self.released_at.clear()
        self.last_direction = None
        self.view = 'switcher'
        self.pending_view = None

    def remove(self, owner):
        self.holds.pop(owner, None)
        self.spent.discard(owner)
        if self.owner == owner:
            self.reset()
            return [('disconnect', None)]
        return []

    def tick(self, owner, state, now):
        if self.mode == 'idle':
            if self.config.gesture <= state.buttons:
                start = self.holds.setdefault(owner, now)
                if owner not in self.spent and now - start >= self.config.hold_seconds:
                    self.spent.add(owner)
                    self.request_open(owner)
                    return [('armed', None)]
            else:
                self.holds.pop(owner, None)
                self.spent.discard(owner)
            return []
        if owner != self.owner:
            return []
        if self.mode == 'arming':
            return [('open', None)] if state.neutral(self.config.dead_zone) else []
        if self.mode == 'closing':
            if state.neutral(self.config.dead_zone):
                action = self.exit_action
                self.reset()
                return [('close', action)]
            return []
        for code in self.held_buttons - state.buttons:
            self.released_at[code] = now
        self.held_buttons = set(state.buttons)
        # Some pads report a 1 ms release while a button is still held; that is not a new press.
        bounced = {code for code, at in self.released_at.items() if now - at < BOUNCE_SECONDS}
        workspace_buttons = state.buttons & {e.BTN_TL, e.BTN_TR}
        workspace_pressed = workspace_buttons - self.workspace_held - bounced
        self.workspace_held = workspace_buttons
        if self.persistent:
            if self.config.gesture <= state.buttons:
                start = self.holds.setdefault(owner, now)
                if now - start >= self.config.hold_seconds:
                    self.request_close('resume')
                    return [('closing', None)]
            else:
                self.holds.pop(owner, None)
            if self.pending_view is not None:
                if state.neutral(self.config.dead_zone):
                    return [('view', self.pending_view)]
                return []
        pressed = state.buttons - self.previous - bounced
        self.previous = set(state.buttons)
        if self.persistent:
            target = None
            if e.BTN_START in pressed:
                target = 'hide' if self.view == 'switcher' else 'switcher'
            elif e.BTN_NORTH in pressed and self.view == 'switcher':
                target = 'keyboard'
            elif self.view == 'switcher' and e.BTN_EAST in pressed:
                target = 'hide'
            elif self.view == 'switcher' and e.BTN_SOUTH in pressed:
                target = 'confirm'
            elif self.view == 'pointer' and e.BTN_NORTH in pressed:
                target = 'keyboard_toggle'
            if target:
                self.request_view(target)
                return [('view_pending', target)]
        elif e.BTN_EAST in pressed:
            self.request_close()
            return [('closing', None)]
        elif not self.persistent and e.BTN_SOUTH in pressed:
            self.request_close('confirm')
            return [('closing', None)]
        actions = []
        if e.BTN_TL in workspace_pressed:
            actions.append(('workspace', -1))
        elif e.BTN_TR in workspace_pressed:
            actions.append(('workspace', 1))
        if self.persistent and self.view == 'pointer':
            return actions
        direction = state.direction(self.config.direction_threshold)
        if direction != self.last_direction:
            self.last_direction = direction
            self.repeat_at = now + self.config.repeat_delay
            if direction:
                actions.append(('navigate', direction))
        elif direction and now >= self.repeat_at:
            self.repeat_at = now + self.config.repeat_interval
            actions.append(('navigate', direction))
        return actions
