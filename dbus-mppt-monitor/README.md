# dbus-mppt-monitor: technical reference

A Venus OS driver that reads a Victron MPPT solar charger over VE.Direct, **read-only**, and publishes it on
D-Bus as a **DC source**, so that the GX's DVCC can't control it. See the [main README](../README.md) for the
why and the step-by-step install.

## How it works

```
MPPT --VE.Direct (text protocol, 1 block/s)--> /dev/ttyS5..7 or ttyUSBx
          |                                         |  opened O_RDONLY by this monitor
          |                                         v
          |                         vedirect_text.TextParser (checksum, skips HEX frames)
          |                                         v
          |                         Telemetry -> History (24 h, 10 s points)
          |                                         v
          |               D-Bus: com.victronenergy.dcsource.mppt_monitor  +  web page :8090
          |
   Victron's vedirect driver on the same port: stopped with serial-starter's stop-tty.sh,
   checked every 10 s (/proc/<pid>/fd and D-Bus), asked again if it comes back
```

* **Reading.** VE.Direct devices send their readings as a text block about once a second, without being asked
  (Victron, *VE.Direct Protocol*, text mode). Blocks end with a checksum byte that makes all bytes add up to 0
  modulo 256. HEX-protocol frames (`:` … newline) can appear between fields; they're skipped and left out of the
  checksum, as in Victron's reference parser. The monitor never sends HEX commands, and an MPPT that
  receives none stays in text mode.
* **Read-only.** `SerialPort` opens the port with `O_RDONLY | O_NOCTTY` and sets 19200 8N1 with `termios`. It
  has no write method, and `tests/test_monitor_only.py` fails if any `O_WRONLY` / `O_RDWR`, `.write()` or
  `.send…()` appears in this folder (apart from the web server's HTTP response).
* **Keeping Victron's driver away.** Venus OS's `serial-starter` runs Victron's VE.Direct driver on every port that
  udev marks `VE_SERVICE=vedirect`. On the Cerbo GX those are the built-in ports `ttyS5`–`ttyS7` (`machine.rules`
  in meta-victronenergy), plus VE.Direct USB cables. `Guard.check()` lists the other processes that have the port
  open (from `/proc/<pid>/fd`). If there are any and `take_over = yes`, it runs
  `/opt/victronenergy/serial-starter/stop-tty.sh <tty>`, at most once every 10 seconds. The main loop also looks
  on D-Bus for a Victron service named after the port (`com.victronenergy.solarcharger.ttyS6`). Either one makes
  the Control status **Victron driver active!**.
* **Not a solar charger.** DVCC adds services of type `solarcharger`, `inverter`/`multi` (inverter/chargers),
  `alternator` and `dcgenset` as chargers (`dbus-systemcalc-py`, `delegates/dvcc.py`, `device_added`), and writes
  their `/Link/*` paths. This monitor registers as `dcsource` by default. systemcalc doesn't use `dcsource` at all,
  and the monitor has no `/Link` paths. `load_config()` refuses DVCC classes and falls back to `dcsource`.
* **Display.** A `dcsource` appears in gui-v2's device list as a DC meter, and on the Overview as a DC source box
  (`data/DcInputs.qml`). The readings also appear as `/GenericInput/*` items, Victron's switch API, which gui-v2
  shows for any service in the switch pane (`src/iochannelgroupmodel.cpp`) and VRM shows under Switches. This needs
  Venus OS v3.80 or newer.

## Settings (`config.ini`)

| Section | Setting | Default | Meaning |
|---|---|---|---|
| `[vedirect]` | `port` | `auto` | `auto`, a port name (`ttyS6`) or a full path (`/dev/serial/by-id/…`). See below |
| | `serial_number` | *(empty)* | Only monitor the MPPT with this serial number (`SER#`, e.g. `HQ2245ABCDE`). Needed with several MPPTs on `auto` |
| | `take_over` | `yes` | Ask serial-starter to stop Victron's driver on the port. `no` only if you blocked it some other way |
| | `timeout` | `5.0` | Seconds without a valid block before the MPPT shows as offline |
| `[dbus]` | `service_class` | `dcsource` | D-Bus service type. DVCC classes are refused |
| | `monitor_mode` | `-1` | `/Settings/MonitorMode` of the DC source (−1 generic) |
| | `device_instance` | `71` | VRM device instance (the Nomadic D1 monitor uses 70) |
| | `custom_name` | `Solar MPPT` | Name in the device list, and of the switch-pane cards |
| | `update_interval` | `1.0` | Seconds between D-Bus updates |
| | `switch_pane` | `yes` | Publish the `/GenericInput` cards |
| `[web]` | `port` | `8090` | Web page port; `0` = no web page |
| | `bind` | `0.0.0.0` | Address to listen on |
| | `password` | *(empty)* | Optional HTTP basic-auth password (any user name) |

**`port = auto`.** Every 10 seconds the monitor looks on D-Bus for a `com.victronenergy.solarcharger.tty…` service
that Victron's driver created for a VE.Direct MPPT, matching `serial_number` if set. If exactly one is found, the
monitor takes that port. Once it has read the MPPT's serial number there, it remembers the port in `state.json`. On
later starts, if Victron's driver hasn't published the MPPT (because it was stopped), the remembered port is used. An
MPPT moved to another port is found again the same way. With a [one-way cable](../README.md#optional-a-one-way-cable),
set the port by name instead.

## Commands

Run on the GX (`Cerbo Setup.bat` does these for you):

| Command | What it does |
|---|---|
| `python3 /data/dbus-mppt-monitor/dbus-mppt-monitor.py --scan` | Lists the VE.Direct ports. For each: which device Victron's driver sees there, which processes have it open, and, for free ports only, a 3-second read-only listen. Never opens a busy port |
| `… --monitor` | Prints the readings once a second. If the service is running, it reads them from the service's web API instead of the port. Otherwise it frees the port from Victron's driver, until the next restart, and reads it |
| `… --simulate` | Pretend MPPT. Registers separately (`com.victronenergy.dcsource.mppt_monitor_sim`, instance 72, web port 8091, `ui-sim.json`), so it can run beside a real installation. Add `--no-dbus` to run it on any PC |
| `… --port ttyS6` | Overrides `port` for this run |
| `svc -t /service/dbus-mppt-monitor` | Restart the service (after editing `config.ini`) |
| `tail -F /var/log/dbus-mppt-monitor/current \| tai64nlocal` | Follow the log |

## D-Bus / MQTT / Node-RED

Service `com.victronenergy.dcsource.mppt_monitor` (MQTT: `N/<portal-id>/dcsource/71/...`). Every value is
read-only. The only writable paths are the switch-pane display settings (`/GenericInput/*/Settings/CustomName`,
`/Group`, `/ShowUIInput`), which are kept in `ui.json`.

| Path | Meaning |
|---|---|
| `/Dc/0/Voltage`, `/Dc/0/Current`, `/Dc/0/Power` | Battery side: V, A (+ = charging), W |
| `/History/EnergyOut` | Total yield (kWh), from `H19` |
| `/Settings/MonitorMode` | −1 (generic DC source) |
| `/Serial`, `/FirmwareVersion` | From `SER#` and `FW`/`FWE` |
| `/Mppt/Online` | 1 while blocks are arriving |
| `/Mppt/Port` | The port being read |
| `/Mppt/Control`, `/Mppt/ControlText` | 0 Monitor only, 1 Victron driver active!, 2 Waiting for the MPPT |
| `/Mppt/State`, `/Mppt/StateText` | `CS`: 0 Off, 2 Fault, 3 Bulk, 4 Absorption, 5 Float, 252 External control… |
| `/Mppt/Error`, `/Mppt/ErrorText` | `ERR`, with the wording of the MPPT manual's error list |
| `/Mppt/TrackerMode`, `/Mppt/TrackerModeText` | `MPPT`: 0 Off, 1 Voltage or current limited, 2 MPP tracking |
| `/Mppt/OffReason`, `/Mppt/OffReasonText` | `OR` bit field and its texts |
| `/Mppt/PvVoltage`, `/Mppt/PvPower` | `VPV` (V), `PPV` (W) |
| `/Mppt/YieldToday`, `/Mppt/YieldYesterday`, `/Mppt/YieldTotal` | `H20`, `H22`, `H19` (kWh) |
| `/Mppt/MaxPowerToday`, `/Mppt/MaxPowerYesterday` | `H21`, `H23` (W) |
| `/Mppt/ProductId` | `PID` (the MPPT model) |
| `/Mppt/Temperature` | `T`, only on models that send it |
| `/GenericInput/<item>/...` | The switch-pane items ([Victron switch API](https://github.com/victronenergy/venus/wiki/dbus#switch)) |

Switch-pane cards: **Solar MPPT** (Control, State, Error, Yield today, Max power today; hidden at first: Yield
yesterday, Yield total, Tracker), **Solar MPPT solar** (Voltage, Power) and **Solar MPPT battery** (Voltage, Current,
Power). Items on a card sort by name. Control and Error turn their tile red (`/Status` 1) when there's a problem.

## Web page and API

`http://<GX-IP>:8090`: live values, Control status, yields, errors and charts (solar power, battery voltage,
charge current, panel voltage) over 10 minutes to 24 hours. Every request other than `GET` gets `405`.

* `GET /api/status`: live values, texts, yields, device identity, the Control status with the processes / D-Bus
  services found on the port, and block counters.
* `GET /api/history?minutes=60`: one point every 10 s, with fields
  `time, pv_power, pv_voltage, battery_voltage, battery_current, battery_power`. Kept in memory for 24 hours.

## Files

| File | Purpose |
|---|---|
| `dbus-mppt-monitor.py` | The monitor: port guard, reader, D-Bus, web, `--scan`, `--monitor`, `--simulate` |
| `vedirect_text.py` | VE.Direct text-protocol parser and decoder (no dependencies) |
| `webui.html` | The web page |
| `config.ini` | Settings |
| `state.json` | Created at run time with `port = auto`: the remembered port and serial number |
| `ui.json` | Created at run time: your switch-pane names, cards and visibility |
| `install.sh` / `uninstall.sh` | Add / remove the service (`install.sh` adds itself to `/data/rc.local`) |
| `service/` | daemontools run scripts |

## Uninstall

```
sh /data/dbus-mppt-monitor/uninstall.sh
rm -r /data/dbus-mppt-monitor
reboot
```

The restart gives the port back to Victron's driver, after which DVCC controls the MPPT again.

## Points to confirm on real hardware

1. That `stop-tty.sh` keeps serial-starter off the port until the next restart (the guard asks again if it doesn't).
2. How long the MPPT stays in "External control" after the GX stops sending it limits (the start-up gap).
3. Whether Victron's driver recognises an MPPT on a one-way cable (the README advises a fixed `port` for that case).
