#!/bin/sh
# Builds venus-data-mppt-monitor.tar.gz: copy it to a USB stick or SD card, put that in the Cerbo and
# restart the Cerbo, and it installs itself (see "Installing from a USB stick" in README.md).
# On Windows, Cerbo Setup.bat option U does the same.
set -e
cd "$(dirname "$0")"
NAME=venus-data-mppt-monitor.tar.gz
tar --exclude=__pycache__ --exclude='state*.json' --exclude='ui*.json' --exclude='*.tmp' \
    -czf "$NAME" dbus-mppt-monitor -C usb rc
echo "Made $NAME. Copy it to the top folder of a USB stick or SD card."
