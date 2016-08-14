======
solard
======

An ACPI Screen and Keyboard backlight controls via Ambient Light Sensor

Small python program that read via als or acpi_als module the Ambient Light
Sensor value and configure the screen and keyboard backlight.

Many thanks to `danieleds <https://github.com/danieleds/Asus-Zenbook-Ambient-Light-Sensor-Controller>`_
and `Perlover <https://github.com/Perlover/Asus-Zenbook-Ambient-Light-Sensor-Controller>`_. They have done all the
hard work, I have just rewritten a python version, that doesn't need to be compiled.

Note that many power manager applications control brightness too and can conflict
with this tool.

Pull request are welcome.

* Free software: Apache license

Installation
============

This software needs a kernel module to work. Recent kernel (>=4.2) have the
acpi_als module and old kernel you can install the out of tree `als module <https://github.com/danieleds/als>`_

The 'als' module can also be installed on new kernel if the new one don't work
as expected. I personally uses the ali one, I don't really understand how to
ensure the sensor is enabled with the acpi_als one

als module installation if needed
---------------------------------

You have to install this module https://github.com/danieleds/als

Under root::

    cd /usr/src && \
      wget https://github.com/danieleds/als/archive/master.tar.gz && \
      tar xvf master.tar.gz
    dkms add -m als -v master
    dkms install -m als -v master
    echo als >>/etc/modules
    echo "blacklist acpi_als" > /etc/modprobe.d/blacklist-acpi_als.conf
    update-initramfs -u

acpi compatibility
------------------

On most asus laptop, the ambient light sensor in not exposed by default because
of `kernel bug in i915 module <http://www.spinics.net/lists/intel-gfx/msg79628.html>`_.

To expose them two methods:

* You can try to set the boot option acpi_osi='!Windows 2012'
(e.g. at the end of GRUB_CMDLINE_LINUX_DEFAULT in /etc/default/grub), then
"sudo update-grub" and then reboot. This will disable the Fn+f5 and fn+f6 keys

* Or you can rebuild your kernel with this workaround: https://lkml.org/lkml/2014/2/11/1032
  and all Fn keys should works.

/sys permissions with udev
--------------------------

To allow non-root user to control als, screen and keyboard backlight without root
priviledge. You can add the following to /etc/udev/rules.d/99-als.conf::

    KERNEL=="asus::kbd_backlight", SUBSYSTEM=="leds", RUN+="/bin/chmod 0666 /sys/class/leds/asus::kbd_backlight/brightness"
    KERNEL=="ACPI0008:00", SUBSYSTEM=="acpi", DRIVER=="als", RUN+="/bin/chmod 0666 /sys/devices/platform/ACPI0008:00/firmware_node/ali /sys/devices/platform/ACPI0008:00/firmware_node/enable"
    KERNEL=="intel_backlight", SUBSYSTEM=="backlight", RUN+="/bin/chmod 666 /sys/class/backlight/intel_backlight/brightness"

And then reload udev rules::

    udevadm control --reload-rules
    udevadm trigger

Usage
-----

Install it::

    pip install --user git+https://github.com/sileht/solard.git#egg=solard


Run it::

    solard --help


My personnal setup
------------------

Tested on an Asus UX303UA, with:

* debian 9 (stretch)
* vanilla kernel 4.7 and the workaround patch
* the danieleds/als kernel module (the acpi_als one doesn't always enable the sensor on startup)
* the udev rules

My desktop uses i3 and mate-desktop. All FN keys works.

My i3 config contains::

    bindcode 248 exec --no-startup-id "bash -c 'pkill solard;solard --stop-on-outside-change;'"
    exec_always --no-startup-id "bash -c 'pkill solard;solard --stop-on-outside-change;'"

This bind the Fn+a (or Fn+q) key to this tool and start this tool on i3
startup.

Using *--stop-on-outside-change* allows to stop the automatic brightness
configuration with the keyboard or the screen brightness keys are used
(Fn+f3 -> Fn+f4).

I aslo disable all brightness feature of mate-power-manager (dim on idle,
Reduce backlight on Battery), to not conflict with this tool.
