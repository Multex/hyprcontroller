"""Forward one gamepad to a stable uinput device, including force feedback."""
import errno
from pathlib import Path

from evdev import UInput, ff, ecodes as e


PREFIX = 'hyprcontroller: '


def identity(device):
    return (device.name, tuple(device.info), device.uniq, device.phys)


def neutral_axis(code, info):
    if code in (e.ABS_Z, e.ABS_RZ, e.ABS_BRAKE, e.ABS_GAS):
        return info.min
    return round((info.min + info.max) / 2)


class Bridge:
    def __init__(self, device):
        self.key = identity(device)
        self.source = None
        self.paused = False
        self.effects = {}
        self.effect_templates = {}
        self.ui = UInput.from_device(
            device, filtered_types=(e.EV_SYN, e.EV_FF_STATUS),
            name=PREFIX + device.name, phys='hyprcontroller/bridge',
            vendor=device.info.vendor, product=device.info.product,
            version=device.info.version, bustype=device.info.bustype)
        self.path = self.ui.device.path
        parent = (Path('/sys/class/input') / Path(self.path).name / 'device').resolve()
        self.nodes = [self.path] + [str(Path('/dev/input') / p.name) for p in parent.glob('js*')]
        self.buttons = set()
        self.axes = {code: info for code, info in device.capabilities().get(e.EV_ABS, [])}
        try:
            self.attach(device)
            self.neutralize()
        except BaseException:
            self.close()
            raise

    def attach(self, device):
        if self.source is not None:
            raise OSError(errno.EBUSY, 'Bridge already has a connected controller')
        device.grab()
        self.source = device
        self.paused = False
        self.effects.clear()
        try:
            for virtual_id, template in self.effect_templates.items():
                effect = ff.Effect.from_buffer_copy(template)
                effect.id = -1
                self.effects[virtual_id] = device.upload_effect(effect, writeback_id=False)
        except BaseException:
            self.detach()
            raise

    def neutralize(self):
        for code in self.buttons:
            self.ui.write(e.EV_KEY, code, 0)
        self.buttons.clear()
        for code, info in self.axes.items():
            self.ui.write(e.EV_ABS, code, neutral_axis(code, info))
        self.ui.syn()
        if self.source:
            for effect in self.effects.values():
                try:
                    self.source.write(e.EV_FF, effect, 0)
                except OSError:
                    pass

    def pause(self):
        self.paused = True
        self.neutralize()

    def resume(self):
        # The mode machine waits for neutral before resuming. Do not replay input.
        self.paused = False

    def forward(self, event):
        if self.paused or self.source is None:
            return
        if event.type == e.EV_SYN and event.code != e.SYN_REPORT:
            return
        if event.type not in (e.EV_KEY, e.EV_ABS, e.EV_SYN):
            return
        if event.type == e.EV_KEY:
            if event.value:
                self.buttons.add(event.code)
            else:
                self.buttons.discard(event.code)
        self.ui.write_event(event)

    def feedback(self, event):
        if event.type == e.EV_UINPUT and event.code == e.UI_FF_UPLOAD:
            upload = self.ui.begin_upload(event.value)
            virtual_id = upload.effect.id
            try:
                template = bytes(upload.effect)
                if self.source is not None:
                    upload.effect.id = self.effects.get(virtual_id, -1)
                    self.effects[virtual_id] = self.source.upload_effect(upload.effect, writeback_id=False)
                self.effect_templates[virtual_id] = template
                upload.retval = 0
            except OSError as exc:
                upload.retval = -(exc.errno or errno.EIO)
            finally:
                upload.effect.id = virtual_id
                self.ui.end_upload(upload)
        elif event.type == e.EV_UINPUT and event.code == e.UI_FF_ERASE:
            erase = self.ui.begin_erase(event.value)
            try:
                real_id = self.effects.pop(erase.effect_id, None)
                self.effect_templates.pop(erase.effect_id, None)
                if self.source is not None and real_id is not None:
                    self.source.erase_effect(real_id)
                erase.retval = 0
            except OSError as exc:
                erase.retval = -(exc.errno or errno.EIO)
            finally:
                self.ui.end_erase(erase)
        elif event.type == e.EV_FF and self.source is not None:
            if event.code in (e.FF_GAIN, e.FF_AUTOCENTER):
                self.source.write(e.EV_FF, event.code, event.value)
            elif event.code in self.effects:
                self.source.write(e.EV_FF, self.effects[event.code], 0 if self.paused else event.value)

    def detach(self):
        self.neutralize()
        source, self.source = self.source, None
        if source:
            for real_id in self.effects.values():
                try:
                    source.erase_effect(real_id)
                except OSError:
                    pass
            try:
                source.ungrab()
            except OSError:
                pass
        self.effects.clear()

    def close(self):
        try:
            self.detach()
        finally:
            self.ui.close()
