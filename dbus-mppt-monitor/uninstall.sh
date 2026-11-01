#!/bin/sh
# Stops the monitor and removes it from start-up. Leaves /data/dbus-mppt-monitor in place.
# Restart the GX afterwards: Venus OS then starts its own VE.Direct driver on the MPPT's port
# again, and DVCC can control the MPPT again.
DIR=/data/dbus-mppt-monitor

sed -i "\#$DIR/install.sh#d" /data/rc.local 2>/dev/null
rm -f /service/dbus-mppt-monitor
svc -dx "$DIR/service" "$DIR/service/log" 2>/dev/null
echo "Uninstalled. Restart the GX to give the VE.Direct port back to Victron's driver."
echo "You can then delete $DIR"
