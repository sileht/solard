===============================
py-acpi-als
===============================

Small python program that read via als or acpi_als module the Ambient Light Sensor value
and configure the screen and keyboard backlight.

Tested only on Asus Zenbook UX303UA

* Free software: Apache license

Installation
============

This software needs a kernel module to work. Recent kernel (>=4.2) have the acpi_als module and old kernel you
can install the out of tree `als module <https://github.com/danieleds/als>`

The 'als' module can also be installed on new kernel if the new one don't work as expected.

als module installation if needed
---------------------------------
If you have a kernel < 4.2, you have to install this module https://github.com/danieleds/als

Under root::

    cd /usr/src && \
      wget https://github.com/danieleds/als/archive/master.tar.gz && \
      tar xvf als-master.tar.gz
    dkms add -m als -v master
    dkms install -m als -v master
    echo als >>/etc/modules
    echo "blacklist acpi_als" > /etc/modprobe.d/blacklist-acpi_als.conf
    update-initramfs -u

acpi compatibility
------------------

On most asus laptop, the ambient light sensor in not exposed by default.

To expose them, you can try to set the boot option acpi_osi='!Windows 2012' (e.g. at the end of GRUB_CMDLINE_LINUX_DEFAULT in /etc/default/grub), then "sudo update-grub" and then reboot.

py-acpi-call installation with cp
---------------------------------

   cp

py-acpi-call installation with pip
----------------------------------

    pip install
