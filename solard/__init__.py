#!/usr/bin/env python3
# Licensed under the Apache License, Version 4.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import argparse
from concurrent import futures
import collections
import ctypes
import enum
import logging
import math
import os
import time
import threading
import signal
import sys
import Xlib.display
import Xlib.Xatom


TRACE = 5
logging.addLevelName(TRACE, 'TRACE')


class LoggerAdapter(logging.LoggerAdapter):
    def trace(self, msg, *args, **kwargs):
        self.log(TRACE, msg, *args, **kwargs)

LOG = LoggerAdapter(logging.getLogger("solard"), {})

LID_SYSPATH = "/proc/acpi/button/lid/LID/state"

SCREEN_BACKLIGHT_SYSPATH = "/sys/class/backlight/"
SUPPORTED_SCREEN_BACKLIGHT_MODULES = ["acpi_video0", "intel_backlight"]

ALS_SYSPATH = "/sys/bus/acpi/drivers/%s/ACPI0008:00"
SUPPORTED_ALS_MODULES = ["acpi_als", "als"]

ALS_INPUT_SYSPATH_MAP = {
    "acpi_als": os.path.join(ALS_SYSPATH % "acpi_als",
                             "iio:device0/in_illuminance_input"),
    "als": os.path.join(ALS_SYSPATH % "als", "ali")
}

KEYBOARD_BACKLIGHT_SYSPATH = "/sys/class/leds/%s/brightness"
SUPPORTED_KEYBOARD_BACKLIGHT_MODULES = ["asus::kbd_backlight"]


xlib = ctypes.cdll.LoadLibrary('libX11.so.6')
xss = ctypes.cdll.LoadLibrary('libXss.so.1')


class XScreenSaverInfo(ctypes.Structure):
    """ typedef struct { ... } XScreenSaverInfo; """
    _fields_ = [('window',      ctypes.c_ulong),  # screen saver window
                ('state',       ctypes.c_int),    # off,on,disabled
                ('kind',        ctypes.c_int),    # blanked,internal,external
                ('since',       ctypes.c_ulong),  # milliseconds
                ('idle',        ctypes.c_ulong),  # milliseconds
                ('event_mask',  ctypes.c_ulong)]  # events


class XScreenSaverQuerier(object):
    # This create two X clients..., but Xlib python binding doesn't have xss
    # extention
    def __init__(self):
        self.dpy = Xlib.display.Display()
        self.screen = self.dpy.screen()
        self.root = self.screen.root

        self.c_dpy = xlib.XOpenDisplay(os.environ['DISPLAY'])
        self.c_root = xlib.XDefaultRootWindow(self.c_dpy)
        xss.XScreenSaverAllocInfo.restype = ctypes.POINTER(XScreenSaverInfo)
        self.c_xss_info = xss.XScreenSaverAllocInfo()

    def get_idle(self):
        active_windows = self.root.get_property(
            self.dpy.get_atom("_NET_ACTIVE_WINDOW"),
            Xlib.Xatom.WINDOW, 0, 4).value
        if active_windows:
            win = self.dpy.create_resource_object('window', active_windows[0])
            size = win.get_geometry()
            is_fullscreen = (
                size._data["width"] == self.screen.width_in_pixels and
                size._data["height"] == self.screen.height_in_pixels
            )
            if is_fullscreen:
                LOG.debug("Fullscreen App detected, no dim")
                return 0

        xss.XScreenSaverQueryInfo(self.c_dpy, self.c_root, self.c_xss_info)
        return self.c_xss_info.contents.idle


class BacklightsChangedOutside(Exception):
    pass


class State(enum.Enum):
    Used = 0
    Idle = 1
    Closed = 2


class LoopThread(threading.Thread):
    def __init__(self, method, interval):
        self.method = method
        self.interval = interval
        self._shutdown = threading.Event()
        self._t = threading.Thread(target=self._loop)
        self._t.start()

    def _loop(self):
        while not self._shutdown.is_set():
            try:
                self.method()
            except Exception:
                LOG.exception("Something wrong append, retrying later.")
            self._shutdown.wait(self.interval)

    def stop(self):
        self._shutdown.set()

    def wait(self):
        self._t.join()


class Daemon(object):
    def __init__(self, conf):
        self.conf = conf
        # Set additionnal static configuration
        self.conf.screen_brightness_max = self.get_screen_brightness_max()

        self.last_screen_brightness = self.get_screen_brightness()
        self.last_keyboard_brightness = self.get_keyboard_brightness()
        # Calculate previous value from the screen brightness
        self.ambient_light_last = (self.last_screen_brightness * 100 /
                                   self.conf.screen_brightness_max)
        if self.ambient_light_last < self.conf.screen_brightness_min:
            self.ambient_light_last = 0
        self.ambient_light_current = self.ambient_light_last
        self.ambient_light_values = collections.deque(
            maxlen=self.conf.ambient_light_measures_number)

        self.brightnesses_to_set = (0, 0)
        self.brightnesses_have_to_change = threading.Event()

        self.was_already_idle = False
        self.xscreensaver_querier = XScreenSaverQuerier()

        self._threads = []
        self._shutdown = threading.Event()

        self._state = State.Used

    def idle(self):
        if self.conf.idle_dim <= 0:
            return False
        return self.xscreensaver_querier.get_idle() > self.conf.idle_dim * 1000

    def _spawn(self, method, interval):
        self._threads.append(LoopThread(method, interval))

    def run(self):
        def stop(signum, stack):
            self._shutdown.set()

        signal.signal(signal.SIGINT, stop)
        signal.signal(signal.SIGTERM, stop)

        self._spawn(self.event_detection_thread,
                    self.conf.update_interval)
        self._spawn(self.brightness_update_thread, 0)

        # .wait() won't work well with signal...
        while not self._shutdown.is_set():
            time.sleep(0.5)

        LOG.debug("Exiting...")
        for t in self._threads:
            t.stop()
        for t in self._threads:
            t.wait()

    def event_detection_thread(self):
        if self.lid_is_closed():
            if self._state != State.Closed:
                LOG.info("LID closed")
                self.brightnesses_set(0, 0)
                self._state = State.Closed
        elif self.idle():
            if self._state != State.Idle:
                self.verify_if_something_changed_outside()
                LOG.info("User idle detected")
                self.brightnesses_set(
                    self.conf.screen_brightness_dim_min, 100)
                self._state = State.Idle
            self.update_ambient_light_tendency()
        elif self._state != State.Used:
            if self._state == State.Closed:
                LOG.info("LID opened")
            elif self._state == State.Idle:
                LOG.info("User back detected")
            self._state = State.Used

            self.update_ambient_light_tendency()
            self.brightnesses_set(self.ambient_light_last,
                                  self.ambient_light_last)
        else:
            self.verify_if_something_changed_outside()
            self.update_ambient_light_tendency()
            changed_enough = (
                abs(self.ambient_light_current - self.ambient_light_last)
                > self.conf.ambient_light_delta_update
            )
            if changed_enough:
                self.brightnesses_set(self.ambient_light_values[-1],
                                      self.ambient_light_values[-1])
                self.ambient_light_last = self.ambient_light_values[-1]

    def update_ambient_light_tendency(self):
        self.ambient_light_values.append(self.get_ambient_light())
        # Perhaps do better than simple mean
        values = list(self.ambient_light_values)
        if len(values) >= 3:
            values.remove(max(values))
            values.remove(min(values))
        self.ambient_light_current = sum(values) / len(values)
        LOG.trace("self.ambient_light_currents of %s: %s" %
                  (values, self.ambient_light_current))

    def brightnesses_set(self, scr, kbd):
        self.brightnesses_to_set = (scr, kbd)
        self.brightnesses_have_to_change.set()

    def brightness_update_thread(self):
        self.brightnesses_have_to_change.wait(
            timeout=self.conf.update_interval)
        if self.brightnesses_have_to_change.is_set():
            scr, kbd = self.brightnesses_to_set
            LOG.info("Update scr:%s, kbd:%s" % (scr, kbd))
            with futures.ThreadPoolExecutor(max_workers=20) as executor:
                futs = [
                    executor.submit(self.fade_keyboard_brightness, kbd),
                    executor.submit(self.fade_screen_brightness, scr),
                ]
                futures.wait(futs)
                for fut in futs:
                    fut.result()
            self.brightnesses_have_to_change.clear()

    @staticmethod
    def read_sys_value(path):
        LOG.trace("cat %s" % path)
        with open(path) as f:
            return f.read().strip()

    @staticmethod
    def write_sys_value(path, value):
        LOG.trace("echo %s > %s" % (value, path))
        with open(path, 'w') as f:
            f.write(value)

    @classmethod
    def lid_is_closed(cls):
        value = cls.read_sys_value(LID_SYSPATH)
        return value == "closed"

    def setup_logging(self):
        if self.conf.log:
            logging.basicConfig(filename=self.conf.log, level=logging.DEBUG)
            LOG.debug("Log level set to DEBUG")
        else:
            if self.conf.debug:
                level = TRACE
            elif self.conf.verbose:
                level = logging.DEBUG
            elif self.conf.quiet:
                level = logging.ERROR
            else:
                level = logging.INFO
            logging.basicConfig(level=level)

    def enable_ambient_light(self):
        if self.conf.ambient_light_sensor != "als":
            return
        LOG.debug("Enable als ambient light")
        path = os.path.join(ALS_SYSPATH, "enable") % "als"
        try:
            self.write_sys_value(path, "1")
        except IOError:
            LOG.error("Fail to enable ambient light sensor, "
                      "are udev rules configured correctly ?")
        # Ensure next read value will be up to date
        time.sleep(0.2)

    def get_ambient_light(self):
        # This mapping have been done for Asus Zenbook UX303UA, but according
        # https://github.com/danieleds/Asus-Zenbook-Ambient-Light-Sensor-Controller/blob/master/service/main.cpp
        # previous/other Zenbook can report only 5 raws
        path = ALS_INPUT_SYSPATH_MAP[self.conf.ambient_light_sensor]
        try:
            raw = int(self.read_sys_value(path))
        except IOError:
            LOG.error("Fail to read ambient light sensor value, "
                      "are udev rules configured correctly ?")
            raw, normalized = None, 100
        else:
            LOG.trace("Get ambient light (raw): %s)" % raw)
            if raw > 0:
                normalized = min(math.log10(raw)
                                 / self.conf.ambient_light_factor
                                 * 100.0, 100)
            else:
                normalized = 0
            LOG.debug("Get ambient light: %s (%s)" % (normalized, raw))
        if normalized < self.conf.screen_brightness_min:
            normalized = self.conf.screen_brightness_min
        return normalized

    def get_screen_brightness_max(self):
        value = int(self.read_sys_value(
            os.path.join(SCREEN_BACKLIGHT_SYSPATH, self.conf.screen_backlight,
                         "max_brightness")))
        LOG.debug("Get screen backlight maximum: %d", value)
        return value

    def get_screen_brightness(self):
        try:
            value = int(self.read_sys_value(os.path.join(
                SCREEN_BACKLIGHT_SYSPATH, self.conf.screen_backlight,
                "brightness")))
        except IOError:
            LOG.error("Fail to get screen brightness, "
                      "are udev rules configured correctly ? ")
        LOG.debug("Current screen backlight: %s" % value)
        return value

    def verify_if_something_changed_outside(self):
        self.verify_if_something_keyboard_changed_outside()
        self.verify_if_something_screen_changed_outside()

    def something_have_changed_outside(self):
        if self.conf.stop_on_outside_change:
            LOG.info("Brightness changed outside, exiting")
            self._shutdown.set()
        else:
            LOG.info("Brightness changed outside, restarting")
            self.brightnesses_set(self.ambient_light_last,
                                  self.ambient_light_last)

    def verify_if_something_keyboard_changed_outside(self):
        keyboard_brightness = self.get_keyboard_brightness()
        changed_outside = keyboard_brightness != self.last_keyboard_brightness
        if changed_outside:
            self.last_keyboard_brightness = keyboard_brightness
            self.something_have_changed_outside()

    def verify_if_something_screen_changed_outside(self):
        screen_brightness = self.get_screen_brightness()
        changed_outside = screen_brightness != self.last_screen_brightness
        if changed_outside:
            self.last_screen_brightness = screen_brightness
            self.something_have_changed_outside()

    def fade_screen_brightness(self, target):
        raw_target = int(self.conf.screen_brightness_max * float(target)
                         / 100.0)
        LOG.debug("Set screen backlight to %d%% (%d%%)" % (target, raw_target))
        screen_brightness = self.get_screen_brightness()

        diff = raw_target - screen_brightness
        if diff == 0:
            return
        elif diff > 0:
            step = 1
            is_finished = lambda: screen_brightness >= raw_target
        else:
            step = -1
            is_finished = lambda: screen_brightness <= raw_target

        interval = abs(self.conf.screen_brightness_time / diff)
        # Sleeping less than 5ms doesn't looks good
        while interval < 0.005:
            interval *= 2
            step *= 2

        LOG.debug("%s -> %s (step:%s, interval: %s)" % (
            screen_brightness, raw_target, step, interval))

        screen_brightness += step
        while not is_finished():
            self.set_screen_brightness(screen_brightness)
            time.sleep(interval)
            screen_brightness += step
        self.set_screen_brightness(raw_target)

    def set_screen_brightness(self, value):
        self.verify_if_something_screen_changed_outside()
        try:
            self.write_sys_value(os.path.join(
                SCREEN_BACKLIGHT_SYSPATH, self.conf.screen_backlight,
                "brightness"
            ), "%d" % value)
        except IOError:
            LOG.error("Fail to set screen brightness, "
                      "are udev rules configured correctly ? ")
        self.last_screen_brightness = value

    def get_keyboard_brightness(self):
        if self.conf.keyboard_backlight is None:
            return 0

        # reading a just written value returns previous value so we sleep a
        # bit...
        time.sleep(0.1)
        try:
            value = int(self.read_sys_value(
                KEYBOARD_BACKLIGHT_SYSPATH % self.conf.keyboard_backlight))
        except IOError:
            LOG.error("Fail to set keyboard backlight, "
                      "are udev rules configured correctly ?")
        LOG.debug("Current keyboard backlight: %s" % value)
        return value

    def fade_keyboard_brightness(self, percent):
        if self.conf.keyboard_backlight is None:
            return
        # NOTE(sileht): we currently support only the asus one
        # so we assume value 0 to 3 are the correct range
        enabled = percent < self.conf.keyboard_backlight_threshold
        targets = range(1, 4) if enabled else range(2, -1, -1)

        if targets[-1] == self.last_keyboard_brightness:
            return

        LOG.debug("Set keyboard backlight to %s", targets[-1])
        for target in targets:
            self.set_keyboard_brightness(target)
            time.sleep(self.conf.keyboard_brightness_step_duration)

    def set_keyboard_brightness(self, value):
        self.verify_if_something_keyboard_changed_outside()
        try:
            self.write_sys_value(
                KEYBOARD_BACKLIGHT_SYSPATH % self.conf.keyboard_backlight,
                "%s" % value)
        except IOError:
            LOG.error("Fail to set keyboard backlight, "
                      "are udev rules configured correctly ?")
        self.last_keyboard_brightness = value


def main():
    available_screen_backlight_modules = [
        mod for mod in SUPPORTED_SCREEN_BACKLIGHT_MODULES
        if os.path.exists(os.path.join(SCREEN_BACKLIGHT_SYSPATH, mod))]
    if not available_screen_backlight_modules:
        LOG.error("No supported backlight found (%s)" %
                  SUPPORTED_SCREEN_BACKLIGHT_MODULES)
        sys.exit(1)

    available_als_modules = [
        mod for mod in SUPPORTED_ALS_MODULES
        if os.path.exists(ALS_SYSPATH % mod)
    ]
    if not available_als_modules:
        LOG.error("No support ambient light sensor found (%s)" %
                  SUPPORTED_ALS_MODULES)
        sys.exit(1)

    available_keyboard_backlight_modules = [
        mod for mod in SUPPORTED_KEYBOARD_BACKLIGHT_MODULES
        if os.path.exists(KEYBOARD_BACKLIGHT_SYSPATH % mod)
    ]

    parser = argparse.ArgumentParser(
        description=("Screen and Keyboard backlight controls via "
                     "Ambient Light Sensor ")
    )
    parser.add_argument('--verbose', '-v', action='store_true')
    parser.add_argument('--debug', '-d', action='store_true')
    parser.add_argument('--quiet', '-q', action='store_true')
    parser.add_argument('--log', help=("log file, disable stdout output and "
                                       "set log level to DEBUG"))
    parser.add_argument("--stop-on-outside-change", action='store_true',
                        help=("If brightness is changed outside the "
                              "daemon stop."))
    parser.add_argument("--update-interval", "-i",
                        default=2.0,
                        type=float,
                        help="Interval between brightness update")

    # Dim configuration
    group = parser.add_argument_group("idle dim arguments")
    group.add_argument("--idle-dim",
                       default=0,
                       type=float,
                       help=("Idle time before dim screen in seconds. "
                             "(0 to disable)"))
    group.add_argument("--screen-brightness-dim-min",
                       default=5,
                       type=int,
                       help=("Minimal percent of allowed brightness for "
                             "idle dim"))

    # Ambient light sensor configuration
    group = parser.add_argument_group("ambient light sensor adjustments")
    group.add_argument("--ambient-light-factor", "-f",
                       default=5.5,
                       type=float,
                       help="Ambient Light to brightness factor")
    group.add_argument("--ambient-light-delta-update", "-u",
                       default=3,
                       type=int,
                       help=("Minimun Ambient Light Sensor percentage delta "
                             "before really change the brightness"))
    group.add_argument("--ambient-light-measures-number",
                       default=5,
                       type=int,
                       help=("Number of ambient light measures to take to "
                             "calculate the brighness"))
    group.add_argument("--ambient-light-measures-interval",
                       default=0.2,
                       type=float,
                       help=("Interval between ambient light measures "
                             "acquisiston."))
    # Brightness update configuration
    group = parser.add_argument_group("brightness smooth update configuration")
    group.add_argument("--screen-brightness-min", "-m",
                       default=5,
                       type=int,
                       help="Minimal percent of allowed brightness")
    group.add_argument("--screen-brightness-time", "-t",
                       default=0.5,
                       type=float,
                       help="Duration of screen brightness change in seconds")
    group.add_argument("--keyboard-backlight-threshold",
                       default=10,
                       type=float,
                       help="Keyboard backlight activation threshold (0-100)")
    group.add_argument("--keyboard-brightness-step-duration",
                       default=0.005,
                       type=float,
                       help="Duration between keyboard brightness step")

    # Drivers config
    group = parser.add_argument_group("drivers selections")
    group.add_argument("--screen-backlight", "-s",
                       default=available_screen_backlight_modules[0],
                       choices=available_screen_backlight_modules,
                       help="Screen backlight kernel module")
    group.add_argument("--keyboard-backlight", "-k",
                       default=(available_keyboard_backlight_modules[0] if
                                available_keyboard_backlight_modules else 0),
                       choices=available_keyboard_backlight_modules,
                       help="Keyboard backlight kernel module")
    group.add_argument("--ambient-light-sensor", "-a",
                       default=available_als_modules[0],
                       choices=available_als_modules,
                       help="Ambient Light Sensor kernel module")

    conf = parser.parse_args()
    daemon = Daemon(conf)
    daemon.setup_logging()
    daemon.enable_ambient_light()
    daemon.run()


if __name__ == '__main__':
    main()
