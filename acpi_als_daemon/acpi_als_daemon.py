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
import ctypes
from datetime import datetime
import logging
import math
import os
import time
import threading
import sys


TRACE = 5
logging.addLevelName(TRACE, 'TRACE')

class LoggerAdapter(logging.LoggerAdapter):
    def trace(self, msg, *args, **kwargs):
        self.log(TRACE, msg, *args, **kwargs)


LOG = LoggerAdapter(logging.getLogger("acpi-als-daemon"), {})

LID_SYSPATH = "/proc/acpi/button/lid/LID/state"

SCREEN_BACKLIGHT_SYSPATH = "/sys/class/backlight/"
SUPPORTED_SCREEN_BACKLIGHT_MODULES = ["acpi_video0", "intel_backlight"]

ALS_SYSPATH = "/sys/bus/acpi/drivers/%s/ACPI0008:00"
SUPPORTED_ALS_MODULES = ["acpi_als", "als"]

ALS_INPUT_SYSPATH_MAP = {
    "acpi_als": os.path.join(ALS_SYSPATH, "iio:device0/in_illuminance_input") % "acpi_als",
    "als": os.path.join(ALS_SYSPATH, "ali") % "als"
}

KEYBOARD_BACKLIGHT_SYSPATH = "/sys/class/leds/%s/brightness"
SUPPORTED_KEYBOARD_BACKLIGHT_MODULES = ["asus::kbd_backlight"]


xlib = ctypes.cdll.LoadLibrary( 'libX11.so.6')
xss = ctypes.cdll.LoadLibrary( 'libXss.so.1')

class XScreenSaverInfo(ctypes.Structure):
    """ typedef struct { ... } XScreenSaverInfo; """
    _fields_ = [('window',      ctypes.c_ulong), # screen saver window
                ('state',       ctypes.c_int),   # off,on,disabled
                ('kind',        ctypes.c_int),   # blanked,internal,external
                ('since',       ctypes.c_ulong), # milliseconds
                ('idle',        ctypes.c_ulong), # milliseconds
                ('event_mask',  ctypes.c_ulong)] # events

class BacklightsChangedOutside(Exception):
    pass


class AcpiAlsDaemon(object):
    def __init__(self, conf):
        self.conf = conf
        # Set additionnal static configuration
        self.conf.screen_brightness_max = self.get_screen_brightness_max()

        self.force_update = True
        self.last_screen_brightness = self.get_screen_brightness()
        self.last_keyboard_brightness = self.get_keyboard_brightness()
        # Calculate previous value from the screen brightness
        self.last_ambient_light = (self.last_screen_brightness * 100 /
                                   self.conf.screen_brightness_max)
        if self.last_ambient_light < self.conf.screen_brightness_min:
            self.last_ambient_light = 0

        self.was_already_idle = False


    def idle(self):
        dpy = xlib.XOpenDisplay(os.environ['DISPLAY'])
        root = xlib.XDefaultRootWindow(dpy)
        xss.XScreenSaverAllocInfo.restype = ctypes.POINTER(XScreenSaverInfo)
        xss_info = xss.XScreenSaverAllocInfo()
        xss.XScreenSaverQueryInfo(dpy, root, xss_info)
        idle_ms = xss_info.contents.idle
        return idle_ms > self.conf.idle_threshold * 1000.0

    def loop(self):
        while True:
            start = datetime.utcnow()

            try:
                if self.lid_is_closed():
                    self.was_already_idle = False
                    if self.last_keyboard_brightness != 0:
                        self.set_keyboard_brightness(0)
                elif self.idle():
                    if not self.was_already_idle:
                        self.raise_if_changed_outside()
                        self.update_all_backlights(0, 0, 100)
                    time.sleep(0.1)
                    self.was_already_idle = True
                    self.force_update = True
                elif self.force_update:
                    self.was_already_idle = False
                    self.update_all_backlights()
                    self.force_update = False
                else:
                    self.was_already_idle = False
                    self.raise_if_changed_outside()
                    ambient_light = self.get_ambient_light()
                    changed_enough = ((abs(ambient_light - self.last_ambient_light) >
                                       self.conf.ambient_light_delta_update))
                    if changed_enough:
                        self.update_all_backlights(ambient_light)
            except BacklightsChangedOutside:
                if self.conf.stop_on_outside_change:
                    LOG.info("Brightness changed outside, exiting")
                    sys.exit(0)
                else:
                    LOG.info("Brightness changed outside, restarting")
                    self.force_update = True
            except Exception:
                LOG.exception("Something wrong append, retrying later.")

            if self.conf.only_once:
                break
            elif not self.force_update:
                elapsed = (datetime.utcnow() - start).total_seconds()
                wait = max(0, self.conf.brightness_update_interval - elapsed)
                time.sleep(wait)

    def update_all_backlights(self, ambient_light=None,
                              screen_pct=None, keyboard_pct=None):
        if ambient_light is None:
            ambient_light = self.get_ambient_light()
        LOG.info("> Change brightness from %d%% to %d%% start" %
                 (self.last_ambient_light, ambient_light))
        with futures.ThreadPoolExecutor(max_workers=20) as executor:
            futs = [
                executor.submit(self.slowly_set_keyboard_brightness,
                                ambient_light if keyboard_pct is None else
                                keyboard_pct),
                executor.submit(self.slowly_set_screen_brightness,
                                ambient_light if screen_pct is None else
                                screen_pct),
            ]
            futures.wait(futs)
            for fut in futs:
                fut.result()
        LOG.info("> Change brightness from %d%% to %d%% finish"  %
                 (self.last_ambient_light, ambient_light))
        self.last_ambient_light = ambient_light

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
        LOG.trace("LID is %s" % value)
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
        path = ALS_INPUT_SYSPATH_MAP[self.conf.ambient_light_sensor]
        try:
            value = int(self.read_sys_value(path))
        except IOError:
            LOG.error("Fail to read ambient light sensor value, "
                    "are udev rules configured correctly ?")
            return 100
        LOG.trace("Get ambient light (raw): %s)" % value)

        # This mapping have been done for Asus Zenbook UX303UA, but according
        # https://github.com/danieleds/Asus-Zenbook-Ambient-Light-Sensor-Controller/blob/master/service/main.cpp
        # previous/other Zenbook can report only 5 values
        if value < 10:
            percent = int(value)
        elif value > 0:
            # Black magic from: https://github.com/Perlover/Asus-Zenbook-Ambient-Light-Sensor-Controller/blob/asus-ux305/service/main.cpp#L225
            # percent = min(int(( math.log( value / 10000.0 * 230 + 0.94 ) * 18 ) /
            #                  10 * 10), 100)
            percent = min(int(math.log10(value) / 5.0 * 100.0 *
                              self.conf.ambient_light_factor), 100)
        else:
            percent = 0
        LOG.debug("Get ambient light (normalized): %s" % percent)
        return percent

    def get_screen_brightness_max(self):
        value = int(self.read_sys_value(
            os.path.join(SCREEN_BACKLIGHT_SYSPATH, self.conf.screen_backlight,
                         "max_brightness")))
        LOG.debug("Get screen backlight maximum: %d", value)
        return value

    def get_screen_brightness(self):
        try:
            value = int(self.read_sys_value(os.path.join(
                SCREEN_BACKLIGHT_SYSPATH, self.conf.screen_backlight, "brightness")))
        except IOError:
            LOG.error("Fail to get screen brightness, "
                    "are udev rules configured correctly ? ")
        LOG.debug("Current screen backlight: %s" % value)
        return value

    def raise_if_changed_outside(self):
        self.raise_if_keyboard_changed_outside()
        self.raise_if_screen_changed_outside()

    def raise_if_keyboard_changed_outside(self):
        keyboard_brightness = self.get_keyboard_brightness()
        changed_outside = keyboard_brightness != self.last_keyboard_brightness
        if changed_outside:
            self.last_keyboard_brightness = keyboard_brightness
            raise BacklightsChangedOutside

    def raise_if_screen_changed_outside(self):
        screen_brightness = self.get_screen_brightness()
        changed_outside = screen_brightness != self.last_screen_brightness
        if changed_outside:
            self.last_screen_brightness = screen_brightness
            raise BacklightsChangedOutside

    def slowly_set_screen_brightness(self, target):
        if target < self.conf.screen_brightness_min:
            target = self.conf.screen_brightness_min
        raw_target = int(self.conf.screen_brightness_max * target / 100)
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

        LOG.debug("%s -> %s (step:%s, interval: %s)" % (screen_brightness, raw_target,
                                                        step, interval))

        screen_brightness += step
        while not is_finished():
            self.set_screen_brightness(screen_brightness)
            time.sleep(interval)
            screen_brightness += step
        self.set_screen_brightness(raw_target)

    def set_screen_brightness(self, value):
        self.raise_if_screen_changed_outside()
        try:
            self.write_sys_value(os.path.join(
                SCREEN_BACKLIGHT_SYSPATH, self.conf.screen_backlight, "brightness"
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

    def slowly_set_keyboard_brightness(self, percent):
        if self.conf.keyboard_backlight is None:
            return
        # NOTE(sileht): we currently support only the asus one
        # so we assume value 0 to 3 are the correct range
        if percent < 10:
            targets = range(1, 4)
        else:
            targets = range(2, -1, -1)

        if targets[-1] == self.last_keyboard_brightness:
            return

        LOG.debug("Set keyboard backlight to %s", targets[-1])
        for target in targets:
            self.set_keyboard_brightness(target)
            time.sleep(self.conf.keyboard_brightness_step_duration)

    def set_keyboard_brightness(self, value):
        self.raise_if_keyboard_changed_outside()
        try:
            self.write_sys_value(KEYBOARD_BACKLIGHT_SYSPATH % self.conf.keyboard_backlight,
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
    parser.add_argument('--log', help="log file, disable stdout output and set log level to DEBUG")

    parser.add_argument('--only-once', action='store_true',
                        help="Set values once and exit.")

    parser.add_argument("--idle-threshold",
                        default=60.0,
                        type=float,
                        help="Idle time before dim screen in seconds")
    parser.add_argument("--brightness-update-interval", "-i",
                        default=2.0,
                        type=float,
                        help="Interval between brightness update")
    parser.add_argument("--stop-on-outside-change", action='store_true',
                        help="If brightness is changed outside the daemon stop.")
    parser.add_argument("--screen-brightness-min", "-m",
                        default=5,
                        type=int,
                        help="Minimal percent of allowed brightness")
    parser.add_argument("--screen-brightness-time", "-t",
                        default=0.2,
                        type=float,
                        help="Duration of screen brightness change in seconds")
    parser.add_argument("--keyboard-brightness-step-duration",
                        default=0.005,
                        type=float,
                        help="Duration between keyboard brightness step")
    parser.add_argument("--screen-backlight", "-s",
                        default=available_screen_backlight_modules[0],
                        choices=available_screen_backlight_modules,
                        help="Screen backlight kernel module")
    parser.add_argument("--keyboard-backlight", "-k",
                        default=(available_keyboard_backlight_modules[0] if
                                 available_keyboard_backlight_modules else 0),
                        choices=available_keyboard_backlight_modules,
                        help="Keyboard backlight kernel module")
    parser.add_argument("--ambient-light-sensor", "-a",
                        default=available_als_modules[0],
                        choices=available_als_modules,
                        help="Ambient Light Sensor kernel module")
    parser.add_argument("--ambient-light-factor", "-f",
                        default=1.5,
                        type=float,
                        help="Ambient Light Sensor percentage factor")
    parser.add_argument("--ambient-light-delta-update", "-u",
                        default=3,
                        type=int,
                        help=("Minimun Ambient Light Sensor percentage delta "
                              "before really change the brightness"))

    conf = parser.parse_args()
    daemon = AcpiAlsDaemon(conf)
    daemon.setup_logging()
    daemon.enable_ambient_light()
    daemon.loop()


if __name__ == '__main__':
    main()
