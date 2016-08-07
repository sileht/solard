===============================
acpi-als-daemon
===============================

ACPI Screen and Keyboard backlight controls via Ambient Light Sensor

Small python program that read via als or acpi_als module the Ambient Light
Sensor value and configure the screen and keyboard backlight.

Many thanks to `danieleds <https://github.com/danieleds/Asus-Zenbook-Ambient-Light-Sensor-Controller>`_
and `Perlover <https://github.com/Perlover/Asus-Zenbook-Ambient-Light-Sensor-Controller>`_. They have done all the
hard work, I have just rewritten a python version, that doesn't need to be compiled.

Tested on my Asus Zenbook UX303UA

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

On most asus laptop, the ambient light sensor in not exposed by default.

To expose them, you can try to set the boot option acpi_osi='!Windows 2012'
(e.g. at the end of GRUB_CMDLINE_LINUX_DEFAULT in /etc/default/grub), then
"sudo update-grub" and then reboot.

py-acpi-call installation with cp
---------------------------------

   cp acpi_als_daemon/acpi_als_daemon.py /usr/local/bin/acpi-als-daemon
   # Test it with :
   acpi-als-daemon -v
   # Start it on boot
   echo "nohup /usr/local/bin/acpi-als-daemon >/dev/null 2>&1 &"


py-acpi-call installation with pip
----------------------------------

    coming soon
