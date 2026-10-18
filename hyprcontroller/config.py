import json
import math
import os
from dataclasses import dataclass
from pathlib import Path
from evdev import ecodes


@dataclass(frozen=True)
class Config:
    input_mode: str = 'observe'
    controller: str = 'auto'
    monitor: str = ''
    activation_buttons: tuple = ('BTN_THUMBL', 'BTN_THUMBR')
    hold_seconds: float = 1.0
    dead_zone: float = 0.24
    direction_threshold: float = 0.55
    repeat_delay: float = 0.35
    repeat_interval: float = 0.12
    pointer_speed: float = 900.0
    scroll_speed: float = 8.0
    keyboard_config: str = 'end4-pC'

    @property
    def gesture(self):
        return frozenset(ecodes.ecodes[name] for name in self.activation_buttons)

    @property
    def activation_hint(self):
        names = {'BTN_THUMBL': 'L3', 'BTN_THUMBR': 'R3', 'BTN_TL': 'LB',
                 'BTN_TR': 'RB', 'BTN_MODE': 'Guide', 'BTN_SELECT': 'Back',
                 'BTN_START': 'Start', 'BTN_SOUTH': 'A', 'BTN_EAST': 'B'}
        buttons = ' + '.join(names.get(b, b.removeprefix('BTN_')) for b in self.activation_buttons)
        return f'{buttons}  ·  Hold {self.hold_seconds:g}s'

    @classmethod
    def load(cls, path=None):
        path = Path(path) if path else Path(os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config')) / 'hyprcontroller/overlay.json'
        values = json.loads(path.read_text()) if path.exists() else {}
        if not isinstance(values, dict):
            raise ValueError('Configuration must be a JSON object')
        if set(values) - set(cls.__dataclass_fields__):
            raise ValueError('Unknown configuration option')
        if 'activation_buttons' in values:
            if not isinstance(values['activation_buttons'], list):
                raise ValueError('Activation buttons must be a list of button names')
            values['activation_buttons'] = tuple(values['activation_buttons'])
        cfg = cls(**values)
        if cfg.input_mode not in ('observe', 'bridge'):
            raise ValueError('Input mode must be observe or bridge')
        if not isinstance(cfg.controller, str) or not isinstance(cfg.monitor, str):
            raise ValueError('Controller and monitor must be strings')
        if not isinstance(cfg.keyboard_config, str) or not cfg.keyboard_config:
            raise ValueError('Keyboard configuration must be a nonempty Quickshell configuration name')
        for name, low, high in (('pointer_speed', 50, 3000), ('scroll_speed', 1, 30)):
            value = getattr(cfg, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
                raise ValueError(f'{name} must be between {low} and {high}')
        if not cfg.activation_buttons or any(not isinstance(b, str) or not b.startswith('BTN_') or b not in ecodes.ecodes for b in cfg.activation_buttons):
            raise ValueError('Activation buttons must be evdev BTN_* names')
        for name in ('hold_seconds', 'repeat_delay', 'repeat_interval'):
            value = getattr(cfg, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0.05 <= value <= 5:
                raise ValueError(f'{name} must be between 0.05 and 5 seconds')
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in (cfg.dead_zone, cfg.direction_threshold)):
            raise ValueError('Dead zone and direction threshold must be finite numbers')
        if not 0 <= cfg.dead_zone < cfg.direction_threshold <= 1:
            raise ValueError('Expected 0 <= dead_zone < direction_threshold <= 1')
        return cfg
