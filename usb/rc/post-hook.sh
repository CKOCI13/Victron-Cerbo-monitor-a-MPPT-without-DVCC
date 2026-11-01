#!/bin/sh
# Installs the service after a USB-stick / SD-card install.
#
# At boot, Venus OS looks for venus-data.tar.gz or venus-data-*.tar.gz (or .tgz / .zip) on any USB
# stick or SD card, unpacks it into /data, and then runs rc/post-hook.sh from the archive with
# "success" or "extraction-failed" as its argument. The rc/ folder itself is not copied to /data.
# Source: update-data.sh in Victron's meta-victronenergy (meta-venus/recipes-core/initscripts).
#
# This runs early in the boot. install.sh adds itself to /data/rc.local, which runs again later
# in the same boot, so the service starts on this boot even if /service isn't ready yet.
[ "$1" = "success" ] || exit 0
sh /data/dbus-mppt-monitor/install.sh

# Install once: rename the file on the stick, so later restarts with the stick still in don't
# unpack it again (and put its config.ini back). If the stick is read-only, nothing changes.
for f in /media/*/venus-data-mppt-monitor.tar.gz; do
    [ -f "$f" ] && mv -f "$f" "$f.installed"
done
sync
exit 0
