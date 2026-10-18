"""A session-owned virtual mouse with proportional stick motion."""
import math
from evdev import UInput, ecodes as e


class DesktopPointer:
    def __init__(self, config, device=None):
        self.config = config
        self.device = device if device is not None else UInput(
            {e.EV_KEY: [e.BTN_LEFT, e.BTN_RIGHT],
             e.EV_REL: [e.REL_X, e.REL_Y, e.REL_WHEEL, e.REL_HWHEEL]},
            name='hyprcontroller desktop pointer', phys='hyprcontroller/pointer')
        self.buttons = set()
        self.fractions = {}
        self.last_tick = None

    @property
    def path(self):
        return self.device.device.path

    def release(self):
        if self.buttons:
            for code in self.buttons:
                self.device.write(e.EV_KEY, code, 0)
            self.device.syn()
        self.buttons.clear()
        self.fractions.clear()
        self.last_tick = None

    def curved(self, value):
        magnitude = max(0, (abs(value) - self.config.dead_zone) / (1 - self.config.dead_zone))
        return math.copysign(magnitude * magnitude, value)

    def sync_buttons(self, state):
        buttons = {mouse for gamepad, mouse in ((e.BTN_SOUTH, e.BTN_LEFT), (e.BTN_EAST, e.BTN_RIGHT))
                   if gamepad in state.buttons}
        changed = False
        for code in self.buttons ^ buttons:
            self.device.write(e.EV_KEY, code, int(code in buttons))
            changed = True
        self.buttons = buttons
        if changed:
            self.device.syn()

    def update(self, state, now):
        elapsed = 0 if self.last_tick is None else max(0, min(.05, now - self.last_tick))
        self.last_tick = now
        self.sync_buttons(state)
        changed = False
        for axis, relative, speed in ((e.ABS_X, e.REL_X, self.config.pointer_speed),
                                      (e.ABS_Y, e.REL_Y, self.config.pointer_speed),
                                      (e.ABS_RX, e.REL_HWHEEL, self.config.scroll_speed),
                                      (e.ABS_RY, e.REL_WHEEL, -self.config.scroll_speed)):
            value = self.curved(state.normalized(axis))
            if value == 0:
                self.fractions[relative] = 0
                continue
            total = self.fractions.get(relative, 0) + value * speed * elapsed
            whole = math.trunc(total)
            self.fractions[relative] = total - whole
            if whole:
                self.device.write(e.EV_REL, relative, whole)
                changed = True
        if changed:
            self.device.syn()

    def close(self):
        try:
            self.release()
        finally:
            self.device.close()
