#!/usr/bin/env python3
"""Venus OS monitor for a Victron MPPT solar charger on VE.Direct, kept out of DVCC.

Monitor only: it opens the VE.Direct port read-only and never sends the MPPT anything, so it
cannot change the MPPT's settings or charging. It also publishes the MPPT as a "DC source"
(not a solar charger), so the GX's DVCC (Distributed Voltage and Current Control) has nothing
to control: DVCC only controls solar chargers, inverter/chargers, alternators and DC gensets.

Venus OS normally starts Victron's own VE.Direct driver on every VE.Direct port. That driver
registers the MPPT as a solar charger, which DVCC then controls. This monitor asks Venus OS's
serial-starter to stop Victron's driver on the MPPT's port (stop-tty.sh), keeps checking that it
stays stopped, and reads the port itself.

Usage:
  dbus-mppt-monitor.py              run as a service (D-Bus + web page)
  dbus-mppt-monitor.py --scan       show the VE.Direct ports and what is on each
  dbus-mppt-monitor.py --monitor    print the MPPT's readings in the terminal
  dbus-mppt-monitor.py --simulate   run against a pretend MPPT (no serial port at all)
  dbus-mppt-monitor.py --no-dbus    web page without D-Bus (any Linux PC)
"""

import argparse
import base64
import collections
import configparser
import glob
import hmac
import json
import logging
import math
import os
import re
import select
import signal
import subprocess
import sys
import termios
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import vedirect_text as vt

VERSION = '1.0.0'
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
VELIB_PATHS = (
    os.path.join(BASE_DIR, 'ext', 'velib_python'),
    '/opt/victronenergy/dbus-systemcalc-py/ext/velib_python',
)
VERSION_FILE = '/opt/victronenergy/version'
STOP_TTY = '/opt/victronenergy/serial-starter/stop-tty.sh'
PROC = '/proc'
DEV = '/dev'
HISTORY_INTERVAL = 10.0               # seconds between history points
HISTORY_POINTS = 24 * 3600 // 10      # 24 hours
GUARD_INTERVAL = 10.0                 # seconds between checks that Victron's driver stays off the port
BAUD = termios.B19200                 # VE.Direct: 19200 bit/s, 8N1

# Service classes that the GX's DVCC controls (dbus-systemcalc-py, delegates/dvcc.py). The
# monitor refuses to register as any of these: that would hand the MPPT's readings to DVCC.
DVCC_CLASSES = ('solarcharger', 'alternator', 'dcgenset', 'inverter', 'multi', 'vebus',
                'charger', 'acsystem')

log = logging.getLogger('mppt-monitor')

DEFAULTS = {
    'vedirect': {
        'port': 'auto',
        'serial_number': '',
        'take_over': 'yes',
        'timeout': '5.0',
    },
    'dbus': {
        'service_class': 'dcsource',
        'monitor_mode': '-1',
        'device_instance': '71',
        'custom_name': 'Solar MPPT',
        'update_interval': '1.0',
        'switch_pane': 'yes',
    },
    'web': {
        'port': '8090',
        'bind': '0.0.0.0',
        'password': '',
    },
}


def load_config(path):
    cfg = configparser.ConfigParser(inline_comment_prefixes=(';', '#'))
    cfg.read_dict(DEFAULTS)
    if os.path.exists(path):
        cfg.read(path)
    else:
        log.warning('%s not found, using built-in defaults', path)
    cls = cfg['dbus']['service_class'].strip().lower()
    if not re.match(r'^[a-z][a-z0-9]*$', cls) or cls in DVCC_CLASSES:
        log.error('service_class = %s is not allowed (DVCC controls that class, or the name is '
                  'invalid); using dcsource', cls)
        cls = 'dcsource'
    cfg['dbus']['service_class'] = cls
    return cfg


def load_json(path):
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        log.warning('Could not read %s: %s', path, e)
        return {}


def save_json(path, data):
    tmp = path + '.tmp'
    try:
        with open(tmp, 'w') as f:
            json.dump(data, f, indent=1)
        os.replace(tmp, path)
    except OSError as e:
        log.warning('Could not save %s: %s', path, e)


def start_thread(name, target, *args):
    def run():
        try:
            target(*args)
        except Exception:
            log.exception('%s thread failed; exiting so the service restarts', name)
            os._exit(1)
    t = threading.Thread(target=run, name=name, daemon=True)
    t.start()
    return t


# --------------------------------------------------------------------------- telemetry

HISTORY_FIELDS = ('pv_power', 'pv_voltage', 'battery_voltage', 'battery_current')


class Telemetry:
    """Latest decoded block from the MPPT, plus running averages for the history."""

    def __init__(self, timeout):
        self.timeout = timeout
        self._lock = threading.Lock()
        self._values = {}
        self._raw = {}
        self._time = None
        self._sums = dict.fromkeys(HISTORY_FIELDS, 0.0)
        self._samples = 0
        self.blocks = 0
        self.parser = vt.TextParser()

    def feed_bytes(self, data):
        for block in self.parser.feed(data):
            self.feed_block(block)

    def feed_block(self, raw):
        values = vt.decode(raw)
        with self._lock:
            self._raw.update(raw)
            self._values.update(values)
            self._time = time.monotonic()
            self.blocks += 1
            current = self._values
            if all(current.get(k) is not None for k in HISTORY_FIELDS):
                for k in HISTORY_FIELDS:
                    self._sums[k] += current[k]
                self._samples += 1

    def take_averages(self):
        """Average of every reading since the last call, or None if none arrived."""
        with self._lock:
            n, sums = self._samples, self._sums
            self._sums, self._samples = dict.fromkeys(HISTORY_FIELDS, 0.0), 0
        if not n:
            return None
        return {k: sums[k] / n for k in HISTORY_FIELDS}

    def snapshot(self):
        now = time.monotonic()
        with self._lock:
            t = self._time
            values = dict(self._values)
            raw = dict(self._raw)
            blocks = self.blocks
        age = None if t is None else now - t
        online = age is not None and age <= self.timeout
        return {
            'online': online,
            'values': values if online else None,
            'last': values,          # last known, also when offline (serial number, firmware)
            'raw': raw,
            'age': age,
            'blocks': blocks,
            'bad_blocks': self.parser.bad_blocks,
        }


class History:
    """The last 24 hours of readings, one averaged point every HISTORY_INTERVAL seconds.

    Kept in memory only, so it starts empty after a restart.
    """

    FIELDS = ('time', 'pv_power', 'pv_voltage', 'battery_voltage', 'battery_current',
              'battery_power')

    def __init__(self, telemetry, interval=HISTORY_INTERVAL, size=HISTORY_POINTS):
        self.telemetry = telemetry
        self.interval = interval
        self._points = collections.deque(maxlen=size)
        self._lock = threading.Lock()
        self._next = time.monotonic() + interval

    def tick(self, now=None):
        now = time.monotonic() if now is None else now
        if now < self._next:
            return
        self._next = max(self._next + self.interval, now)
        self.add(self.telemetry.take_averages())

    def add(self, avg, wall_time=None):
        t = int(time.time() if wall_time is None else wall_time)
        if avg is None:   # offline: a gap in the charts
            point = (t,) + (None,) * (len(self.FIELDS) - 1)
        else:
            point = (t, round(avg['pv_power']), round(avg['pv_voltage'], 2),
                     round(avg['battery_voltage'], 2), round(avg['battery_current'], 2),
                     round(avg['battery_voltage'] * avg['battery_current']))
        with self._lock:
            self._points.append(point)

    def points(self, seconds=None):
        with self._lock:
            points = list(self._points)
        if seconds is not None and points:
            start = points[-1][0] - seconds
            points = [p for p in points if p[0] >= start]
        return points


# --------------------------------------------------------------------------- the serial port

def device_path(port):
    """'ttyS6' -> '/dev/ttyS6'; full paths (including /dev/serial/by-id/...) are kept."""
    port = port.strip()
    return port if port.startswith('/') else os.path.join(DEV, port)


def tty_name(path):
    """The kernel name of a port ('ttyS6', 'ttyUSB0'), following /dev/serial/by-id links."""
    return os.path.basename(os.path.realpath(path))


class SerialPort:
    """Read-only VE.Direct port.

    The port is opened O_RDONLY, so the operating system itself refuses any attempt to write to
    it. There is deliberately no write method.
    """

    def __init__(self, path):
        self.path = path
        self.name = tty_name(path)
        fd = os.open(path, os.O_RDONLY | os.O_NOCTTY | os.O_NONBLOCK)
        try:
            attrs = termios.tcgetattr(fd)
            # raw 8N1, no flow control, receiver on
            attrs[0] = 0                                               # iflag
            attrs[1] = 0                                               # oflag
            attrs[2] = termios.CS8 | termios.CREAD | termios.CLOCAL     # cflag
            attrs[3] = 0                                               # lflag
            attrs[4] = attrs[5] = BAUD                                 # ispeed, ospeed
            attrs[6][termios.VMIN] = 0
            attrs[6][termios.VTIME] = 0
            termios.tcsetattr(fd, termios.TCSANOW, attrs)
        except termios.error as e:
            log.warning('Could not set %s to 19200 8N1: %s', path, e)
        self.fd = fd

    def read(self, timeout=0.5):
        """Bytes received within `timeout` seconds (b'' if none)."""
        r, _, _ = select.select([self.fd], [], [], timeout)
        if not r:
            return b''
        try:
            data = os.read(self.fd, 4096)
        except BlockingIOError:
            return b''
        if not data:
            # A USB cable that was unplugged reads as end-of-file.
            raise OSError('%s: end of file (cable unplugged?)' % self.path)
        return data

    def close(self):
        try:
            os.close(self.fd)
        except OSError:
            pass


def port_holders(path, exclude_pid=None):
    """Other processes that have the port open: list of (pid, name).

    Reads /proc/<pid>/fd, so it needs root on a GX (it has it). Never opens the port.
    """
    exclude_pid = os.getpid() if exclude_pid is None else exclude_pid
    target = os.path.realpath(path)
    holders = []
    for pid_dir in glob.glob(os.path.join(PROC, '[0-9]*')):
        pid = int(os.path.basename(pid_dir))
        if pid == exclude_pid:
            continue
        try:
            fds = os.listdir(os.path.join(pid_dir, 'fd'))
        except OSError:
            continue
        for fd in fds:
            try:
                link = os.readlink(os.path.join(pid_dir, 'fd', fd))
            except OSError:
                continue
            if link == target:
                holders.append((pid, process_name(pid_dir)))
                break
    return holders


def process_name(pid_dir):
    try:
        with open(os.path.join(pid_dir, 'cmdline'), 'rb') as f:
            argv = f.read().split(b'\0')
        name = os.path.basename(argv[0].decode('utf-8', 'replace'))
        if name.startswith('python') and len(argv) > 1 and argv[1] and not argv[1].startswith(b'-'):
            name = os.path.basename(argv[1].decode('utf-8', 'replace'))
        if name:
            return name
    except OSError:
        pass
    try:
        with open(os.path.join(pid_dir, 'comm')) as f:
            return f.read().strip()
    except OSError:
        return '?'


def stop_victron_driver(name):
    """Ask Venus OS's serial-starter to stop its driver on this port (and stop probing it).

    Returns True if the request was made. This talks to Venus OS only, never to the MPPT.
    """
    if not os.path.exists(STOP_TTY):
        log.warning('%s not found: cannot ask Venus OS to free %s', STOP_TTY, name)
        return False
    try:
        subprocess.run([STOP_TTY, name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=30)
        return True
    except (OSError, subprocess.SubprocessError) as e:
        log.warning('stop-tty.sh %s failed: %s', name, e)
        return False


class Guard:
    """Keeps Victron's VE.Direct driver off the MPPT's port, and reports whether it is.

    status: 'monitor_only' - we have the port to ourselves and Victron's driver isn't publishing it
            'victron'      - another process has the port open, or Victron's driver publishes it
            'waiting'      - no port yet
    """

    def __init__(self, take_over):
        self.take_over = take_over
        self.path = None
        self.holders = []
        self.victron_services = []   # set from the main loop (D-Bus)
        self._last_request = 0.0
        self._lock = threading.Lock()

    def check(self, path, now=None):
        """Called from the reader thread every GUARD_INTERVAL seconds and before opening."""
        now = time.monotonic() if now is None else now
        holders = port_holders(path)
        with self._lock:
            self.path = path
            self.holders = holders
        if holders and self.take_over and now - self._last_request >= GUARD_INTERVAL:
            self._last_request = now
            log.warning('%s is open by %s; asking Venus OS to stop its driver there',
                        path, ', '.join('%s (pid %d)' % (n, p) for p, n in holders))
            stop_victron_driver(tty_name(path))
        return holders

    def set_victron_services(self, services):
        with self._lock:
            self.victron_services = list(services)

    @property
    def status(self):
        with self._lock:
            if self.path is None:
                return 'waiting'
            if self.holders or self.victron_services:
                return 'victron'
            return 'monitor_only'

    def describe(self):
        with self._lock:
            return {
                'port': self.path,
                'holders': ['%s (pid %d)' % (n, p) for p, n in self.holders],
                'victron_services': list(self.victron_services),
                'take_over': self.take_over,
            }


# --------------------------------------------------------------------------- finding the MPPT

TTY_RE = re.compile(r'^tty(S|USB|ACM)\d+$')


def victron_devices(bus):
    """VE.Direct devices that Victron's own driver publishes on D-Bus.

    Returns a list of dicts: service, type, tty, product, serial. Read-only D-Bus calls.
    """
    devices = []
    for name in bus.list_names():
        name = str(name)
        parts = name.split('.')
        if len(parts) != 4 or parts[:2] != ['com', 'victronenergy'] or not TTY_RE.match(parts[3]):
            continue
        devices.append({
            'service': name,
            'type': parts[2],
            'tty': parts[3],
            'product': _dbus_value(bus, name, '/ProductName'),
            'serial': _dbus_value(bus, name, '/Serial'),
        })
    return devices


def _dbus_value(bus, service, path):
    try:
        value = bus.get_object(service, path).GetValue(dbus_interface='com.victronenergy.BusItem')
    except Exception:
        return None
    # Victron services report "no value" as an empty array.
    if isinstance(value, str):
        return str(value)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value
    return None


def choose_port(devices, serial_wanted, remembered, exists=os.path.exists):
    """Pick the MPPT's port for port = auto.

    1. An MPPT that Victron's driver currently publishes on a VE.Direct port (matching
       serial_number if set). That's how a new MPPT is found, or one moved to another port.
    2. Otherwise the port remembered from last time (Victron's driver stays off it once stopped).
    Returns (path or None, explanation).
    """
    candidates = [d for d in devices if d['type'] == 'solarcharger']
    if serial_wanted:
        candidates = [d for d in candidates if (d.get('serial') or '') == serial_wanted]
    if len(candidates) == 1:
        d = candidates[0]
        return device_path(d['tty']), 'found %s %s on %s' % (d.get('product') or 'MPPT',
                                                             d.get('serial') or '', d['tty'])
    if len(candidates) > 1:
        return None, ('%d MPPTs found (%s): set serial_number in config.ini to the one to monitor'
                      % (len(candidates), ', '.join('%s on %s' % (d.get('serial'), d['tty'])
                                                    for d in candidates)))
    port = (remembered or {}).get('port')
    if port and exists(port):
        if not serial_wanted or remembered.get('serial') in (None, serial_wanted):
            return port, 'using the port from last time (%s)' % port
    if serial_wanted:
        return None, 'no MPPT with serial number %s found yet' % serial_wanted
    return None, 'no MPPT found yet on a VE.Direct port'


class PortChooser:
    """port = auto: finds the MPPT's port from D-Bus and remembers it in state.json."""

    def __init__(self, cfg, state_path, bus_factory):
        self.cfg = cfg
        self.state_path = state_path
        self.state = load_json(state_path)
        self.bus_factory = bus_factory
        self._last_reason = None

    def find(self):
        v = self.cfg['vedirect']
        if v['port'].strip().lower() != 'auto':
            return device_path(v['port'])
        try:
            devices = victron_devices(self.bus_factory())
        except Exception as e:
            log.debug('D-Bus lookup failed: %s', e)
            devices = []
        path, reason = choose_port(devices, v['serial_number'].strip(), self.state)
        if reason != self._last_reason:
            self._last_reason = reason
            (log.info if path else log.warning)('port = auto: %s', reason)
        return path

    def remember(self, path, serial):
        new = {'port': path, 'serial': serial}
        if self.state != new:
            self.state = new
            save_json(self.state_path, new)


# --------------------------------------------------------------------------- reading

class Reader:
    """Reader thread: waits for a port, frees it from Victron's driver, then reads it read-only."""

    RETRY = 5.0

    def __init__(self, guard, telemetry, serial_wanted=''):
        self.guard = guard
        self.telemetry = telemetry
        self.serial_wanted = serial_wanted
        self._path = None
        self._cond = threading.Condition()
        self.port = None
        self.mismatch = None

    def set_path(self, path):
        with self._cond:
            if path != self._path:
                self._path = path
                self._cond.notify_all()

    def _wait_path(self):
        with self._cond:
            while self._path is None:
                self._cond.wait()
            return self._path

    def run(self):
        while True:
            path = self._wait_path()
            if not os.path.exists(path):
                log.warning('%s does not exist (yet); waiting', path)
                time.sleep(self.RETRY)
                continue
            holders = self.guard.check(path)
            if holders and self.guard.take_over:
                # Give serial-starter a moment to stop its driver before opening the port.
                for _ in range(10):
                    time.sleep(0.5)
                    if not port_holders(path):
                        break
            try:
                port = SerialPort(path)
            except OSError as e:
                log.warning('Cannot open %s: %s', path, e)
                time.sleep(self.RETRY)
                continue
            log.info('Reading %s (read-only: nothing is ever sent to the MPPT)', path)
            self.port = port
            try:
                self._read(port, path)
            except OSError as e:
                log.warning('%s: %s', path, e)
            finally:
                port.close()
                self.port = None
            time.sleep(1)

    def _read(self, port, path):
        next_check = time.monotonic() + GUARD_INTERVAL
        while True:
            with self._cond:
                if self._path != path:
                    log.info('Port changed to %s', self._path)
                    return
            data = port.read(0.5)
            if data:
                self.telemetry.feed_bytes(data)
            now = time.monotonic()
            if now >= next_check:
                next_check = now + GUARD_INTERVAL
                self.guard.check(path, now)


class SimulatedMppt:
    """A pretend MPPT for --simulate: produces real VE.Direct text blocks (with checksums and
    the odd HEX frame) once a second. Nothing touches a serial port.

    A pretend day lasts 20 minutes followed by 4 minutes of night, on a 48 V lithium battery,
    with bulk, absorption and float, so every state shows up within half an hour. It is for
    trying out the screens, not for predicting what a real MPPT will do.
    """

    name = 'simulated'
    path = 'simulated'
    DAY, NIGHT = 1200.0, 240.0
    PEAK = 1400.0          # W at noon
    ABSORPTION_V, FLOAT_V = 56.4, 54.4
    ABSORPTION_TIME = 120.0

    MORNING = 240.0        # start the pretend day mid-morning, so there's sun straight away

    def __init__(self, now=None):
        now = time.monotonic() if now is None else now
        self._start = now - self.MORNING
        self._next = now
        self._last = now
        self.v, self.state = 52.2, 3
        self._absorption_since = None
        self.yield_today, self.yield_total, self.max_today = 0.0, 1234.56, 0
        self.yield_yesterday, self.max_yesterday = 4.21, 1180
        self._day = 0
        self._count = 0

    def _sun(self, now):
        t = (now - self._start) % (self.DAY + self.NIGHT)
        day = int((now - self._start) // (self.DAY + self.NIGHT))
        if day != self._day:   # new day: shift the daily counters
            self._day = day
            self.yield_yesterday, self.max_yesterday = round(self.yield_today, 2), self.max_today
            self.yield_today, self.max_today = 0.0, 0
        if t >= self.DAY:
            return 0.0
        return max(0.0, math.sin(math.pi * t / self.DAY)) ** 1.3

    def step(self, now):
        dt = min(max(now - self._last, 0.0), 2.0)
        self._last = now
        sun = self._sun(now)
        available = self.PEAK * sun
        load = 180.0                                    # pretend 48 V loads, in W
        if sun <= 0.0:
            self.state = 0
            power = 0.0
        elif self.state in (0, 3):
            self.state = 3
            power = available
            if self.v >= self.ABSORPTION_V:
                self.state, self._absorption_since = 4, now
        elif self.state == 4:
            power = min(available, load + 300.0 * max(0.0, (self.ABSORPTION_V - self.v) * 5 + 0.3))
            if now - self._absorption_since >= self.ABSORPTION_TIME:
                self.state = 5
        else:
            power = min(available, load + 120.0 * max(0.0, (self.FLOAT_V - self.v) * 5))
        net = power - load
        self.v = min(57.0, max(50.0, self.v + net * 0.000012 * dt))
        if self.state == 4:
            self.v = min(self.v, self.ABSORPTION_V)
        if self.state == 5:
            self.v = min(self.v, self.FLOAT_V)
        self.pv_power = round(power)
        self.current = power / self.v if self.v else 0.0
        self.pv_v = 0.0 if sun <= 0 else 80.0 + 18.0 * sun
        self.yield_today += power * dt / 3600000.0
        self.yield_total += power * dt / 3600000.0
        self.max_today = max(self.max_today, self.pv_power)

    def block(self):
        return vt.encode_block((
            ('PID', '0xA060'), ('FW', '164'), ('SER#', 'HQSIMULATED'),
            ('V', int(self.v * 1000)), ('I', int(self.current * 1000)),
            ('VPV', int(self.pv_v * 1000)), ('PPV', self.pv_power),
            ('CS', self.state), ('MPPT', 2 if self.state == 3 else (1 if self.state else 0)),
            ('OR', '0x00000001' if self.state == 0 else '0x00000000'), ('ERR', 0),
            ('LOAD', 'OFF'), ('IL', 0),
            ('H19', int(self.yield_total * 100)), ('H20', int(self.yield_today * 100)),
            ('H21', self.max_today), ('H22', int(self.yield_yesterday * 100)),
            ('H23', self.max_yesterday), ('HSDS', self._day),
        ))

    def read(self, timeout=0.5):
        delay = self._next - time.monotonic()
        if delay > 0:
            time.sleep(min(delay, timeout))
            if delay > timeout:
                return b''
        now = time.monotonic()
        self._next = max(self._next + 1.0, now - 1.0)
        self.step(now)
        self._count += 1
        data = self.block()
        if self._count % 7 == 0:   # a HEX frame now and then, as a real MPPT may send
            data = b':A0102000543\n' + data
        return data

    def close(self):
        pass


def sim_reader(sim, telemetry):
    while True:
        data = sim.read(0.5)
        if data:
            telemetry.feed_bytes(data)


def simulation_config(cfg):
    """Run beside a real installation without clashing: own D-Bus name, instance, web port, files."""
    cfg['vedirect']['port'] = 'simulated'
    d = cfg['dbus']
    d['device_instance'] = str(d.getint('device_instance') + 1)
    d['custom_name'] = d['custom_name'] + ' (SIMULATED)'
    if cfg['web'].getint('port'):
        cfg['web']['port'] = str(cfg['web'].getint('port') + 1)


# --------------------------------------------------------------------------- D-Bus

# Read-only values (GenericInput) for the GX switch pane, the GX device page and the VRM
# "Switches" panel / app widget. API: https://github.com/victronenergy/venus/wiki/dbus#switch
INPUT_DISCRETE = 0      # GenericInput: integer shown as text through /Settings/Labels
INPUT_VALUE = 1         # GenericInput: number with a unit
SHOW_LOCAL_REMOTE = 6   # /Settings/ShowUIInput default from the API

STATE_CODES = sorted(vt.STATES)
STATE_LABELS = [vt.STATES[c] for c in STATE_CODES] + ['Unknown', 'No communication']
STATE_UNKNOWN, STATE_NO_COMM = len(STATE_CODES), len(STATE_CODES) + 1

ERROR_CODES = sorted(vt.ERRORS)
ERROR_LABELS = ['Error %d: %s' % (c, vt.ERRORS[c]) if c else 'None' for c in ERROR_CODES] + ['Other error']
ERROR_OTHER = len(ERROR_CODES)

GUARD_STATUSES = ('monitor_only', 'victron', 'waiting')
GUARD_LABELS = ['Monitor only (DVCC can\'t reach it)', 'Victron driver active!', 'Waiting for the MPPT']

TRACKER_LABELS = [vt.TRACKER_MODES[m] for m in sorted(vt.TRACKER_MODES)] + ['Unknown']


def state_index(state):
    if state is None:
        return None
    return STATE_CODES.index(state) if state in vt.STATES else STATE_UNKNOWN


def error_index(error):
    if error is None:
        return None
    return ERROR_CODES.index(error) if error in vt.ERRORS else ERROR_OTHER


# The Cerbo screen: three cards. Items on a card sort by name.
#   (channel, name on its card, type, unit, decimals, labels, card)
# card None = the device's own card; 'pv' / 'bat' = "<custom_name> solar" / "battery".
CARDS = {'pv': '%s solar', 'bat': '%s battery'}
INPUTS = (
    ('control', 'Control', INPUT_DISCRETE, None, None, GUARD_LABELS, None),
    ('state', 'State', INPUT_DISCRETE, None, None, STATE_LABELS, None),
    ('error', 'Error', INPUT_DISCRETE, None, None, ERROR_LABELS, None),
    ('yield_today', 'Yield today', INPUT_VALUE, 'kWh', 2, None, None),
    ('max_power_today', 'Max power today', INPUT_VALUE, 'W', 0, None, None),
    ('yield_yesterday', 'Yield yesterday', INPUT_VALUE, 'kWh', 2, None, None),
    ('yield_total', 'Yield total', INPUT_VALUE, 'kWh', 1, None, None),
    ('tracker_mode', 'Tracker', INPUT_DISCRETE, None, None, TRACKER_LABELS, None),
    ('pv_voltage', 'Voltage', INPUT_VALUE, 'V', 1, None, 'pv'),
    ('pv_power', 'Power', INPUT_VALUE, 'W', 0, None, 'pv'),
    ('battery_voltage', 'Voltage', INPUT_VALUE, 'V', 2, None, 'bat'),
    ('battery_current', 'Current', INPUT_VALUE, 'A', 1, None, 'bat'),
    ('battery_power', 'Power', INPUT_VALUE, 'W', 0, None, 'bat'),
)
# Shown on the device page; start hidden in the switch pane to keep the cards short.
HIDDEN_BY_DEFAULT = ('yield_yesterday', 'yield_total', 'tracker_mode')


def _text(unit, digits):
    def fmt(path, value):
        return '---' if value is None else '%.*f%s' % (digits, value, unit)
    return fmt


class UiSettings:
    """Switch-pane names, cards and visibility as changed on the GX, kept across restarts."""

    def __init__(self, path):
        self.path = path
        self.values = load_json(path)

    def get(self, key, default):
        return self.values.get(key, default)

    def set(self, key, value):
        self.values[key] = value
        save_json(self.path, self.values)


class DbusService:
    """Publishes the MPPT as com.victronenergy.<service_class>.mppt_monitor (dcsource by default).

    A dcsource is a DC meter: the GX shows it in the device list and as a DC source on the
    Overview, and DVCC never controls it. Every value is read-only; the only writable paths are
    the switch-pane display settings (names, cards, visibility).
    """

    def __init__(self, cfg, ui_settings, suffix='mppt_monitor'):
        for p in VELIB_PATHS:
            if os.path.isdir(p):
                sys.path.insert(1, p)
                break
        from vedbus import VeDbusService

        self.ui = ui_settings
        d = cfg['dbus']
        self.service_class = d['service_class']
        name = 'com.victronenergy.%s.%s' % (self.service_class, suffix)
        try:
            svc = VeDbusService(name, register=False)
            needs_register = True
        except TypeError:   # velib from before 2024 registers immediately
            svc = VeDbusService(name)
            needs_register = False
        self.svc = svc
        self.name = name

        svc.add_path('/Mgmt/ProcessName', os.path.abspath(__file__))
        svc.add_path('/Mgmt/ProcessVersion', VERSION)
        svc.add_path('/Mgmt/Connection', 'VE.Direct (monitor only)')
        svc.add_path('/DeviceInstance', d.getint('device_instance'))
        svc.add_path('/ProductId', 0xFFFF)
        svc.add_path('/ProductName', 'MPPT solar charger (monitor only)')
        svc.add_path('/CustomName', d['custom_name'])
        svc.add_path('/FirmwareVersion', None)
        svc.add_path('/HardwareVersion', None)
        svc.add_path('/Serial', None)
        svc.add_path('/Connected', 1)

        V, A, W, KWH = _text('V', 2), _text('A', 1), _text('W', 0), _text('kWh', 2)
        for path, fmt in (('/Dc/0/Voltage', V), ('/Dc/0/Current', A), ('/Dc/0/Power', W),
                          ('/History/EnergyOut', KWH)):
            svc.add_path(path, None, gettextcallback=fmt)
        if self.service_class == 'dcsource':
            svc.add_path('/Settings/MonitorMode', d.getint('monitor_mode'))

        for path in ('/Mppt/Online', '/Mppt/Port', '/Mppt/Control', '/Mppt/ControlText',
                     '/Mppt/State', '/Mppt/StateText', '/Mppt/Error', '/Mppt/ErrorText',
                     '/Mppt/TrackerMode', '/Mppt/TrackerModeText', '/Mppt/OffReason',
                     '/Mppt/OffReasonText', '/Mppt/PvVoltage', '/Mppt/PvPower',
                     '/Mppt/YieldToday', '/Mppt/YieldYesterday', '/Mppt/YieldTotal',
                     '/Mppt/MaxPowerToday', '/Mppt/MaxPowerYesterday', '/Mppt/ProductId',
                     '/Mppt/Temperature'):
            svc.add_path(path, None)

        self.switch_pane = d.getboolean('switch_pane')
        if self.switch_pane:
            for spec in INPUTS:
                self._add_input(d['custom_name'], *spec)

        if needs_register:
            svc.register()
        log.info('Registered %s on D-Bus (monitor only; DVCC does not control %s services)',
                 name, self.service_class)

    def _add_input(self, device_name, channel, name, input_type, unit, decimals, labels, card):
        base = '/GenericInput/' + channel
        add = self.svc.add_path
        add(base + '/Name', name)
        add(base + '/Value', None)
        add(base + '/Status', 0)
        add(base + '/Settings/Type', input_type)
        add(base + '/Settings/ValidTypes', 1 << input_type)
        if unit:
            add(base + '/Settings/Unit', unit)
        if decimals is not None:
            add(base + '/Settings/Decimals', decimals)
        if labels:
            add(base + '/Settings/Labels', labels)
        # On a named card the GUI prefixes the device name unless a custom name is set.
        self._add_ui_setting(base + '/Settings/CustomName', name if card else None)
        self._add_ui_setting(base + '/Settings/Group', CARDS[card] % device_name if card else None)
        self._add_ui_setting(base + '/Settings/ShowUIInput',
                             0 if channel in HIDDEN_BY_DEFAULT else SHOW_LOCAL_REMOTE)

    def _add_ui_setting(self, path, default):
        self.svc.add_path(path, self.ui.get(path, default), writeable=True,
                          onchangecallback=self._on_ui_setting)

    def _on_ui_setting(self, path, value):
        if path.endswith(('/CustomName', '/Group')):
            ok = value is None or (isinstance(value, str) and len(value.encode('utf-8')) <= 32)
        else:   # /ShowUIInput bitfield
            ok = isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 15
        if ok:
            self.ui.set(path, value)
        else:
            log.warning('Rejected D-Bus write %s = %r', path, value)
        return ok

    def refresh(self, snap, guard_status, port):
        s = self.svc
        v = snap['values'] or {}
        last = snap['last']
        on = snap['online']

        s['/Dc/0/Voltage'] = v.get('battery_voltage')
        s['/Dc/0/Current'] = v.get('battery_current')
        s['/Dc/0/Power'] = vt.battery_power(v) if on else None
        s['/History/EnergyOut'] = last.get('yield_total')
        s['/FirmwareVersion'] = last.get('firmware')
        s['/Serial'] = last.get('serial')

        control = GUARD_STATUSES.index(guard_status)
        s['/Mppt/Online'] = int(on)
        s['/Mppt/Port'] = port
        s['/Mppt/Control'] = control
        s['/Mppt/ControlText'] = GUARD_LABELS[control]
        s['/Mppt/State'] = v.get('state')
        s['/Mppt/StateText'] = vt.state_text(v.get('state'))
        s['/Mppt/Error'] = v.get('error')
        s['/Mppt/ErrorText'] = vt.error_text(v.get('error'))
        s['/Mppt/TrackerMode'] = v.get('tracker_mode')
        s['/Mppt/TrackerModeText'] = vt.tracker_text(v.get('tracker_mode'))
        s['/Mppt/OffReason'] = v.get('off_reason')
        s['/Mppt/OffReasonText'] = None if not on else '; '.join(vt.off_reason_texts(v.get('off_reason')))
        s['/Mppt/PvVoltage'] = v.get('pv_voltage')
        s['/Mppt/PvPower'] = v.get('pv_power')
        s['/Mppt/YieldToday'] = last.get('yield_today')
        s['/Mppt/YieldYesterday'] = last.get('yield_yesterday')
        s['/Mppt/YieldTotal'] = last.get('yield_total')
        s['/Mppt/MaxPowerToday'] = last.get('max_power_today')
        s['/Mppt/MaxPowerYesterday'] = last.get('max_power_yesterday')
        s['/Mppt/ProductId'] = last.get('product_id')
        s['/Mppt/Temperature'] = v.get('temperature')

        if self.switch_pane:
            self._refresh_switch_pane(snap, control)

    def _refresh_switch_pane(self, snap, control):
        s = self.svc
        v = snap['values'] or {}
        last = snap['last']
        on = snap['online']
        g = '/GenericInput/%s/Value'

        s[g % 'control'] = control
        s['/GenericInput/control/Status'] = 1 if control == 1 else 0
        s[g % 'state'] = state_index(v.get('state')) if on else STATE_NO_COMM
        s['/GenericInput/state/Status'] = 0 if on else 1
        err = v.get('error')
        s[g % 'error'] = error_index(err)
        s['/GenericInput/error/Status'] = 1 if err else 0
        mode = v.get('tracker_mode')
        s[g % 'tracker_mode'] = (None if mode is None else
                                 sorted(vt.TRACKER_MODES).index(mode) if mode in vt.TRACKER_MODES
                                 else len(vt.TRACKER_MODES))
        for key in ('pv_voltage', 'pv_power', 'battery_voltage', 'battery_current'):
            s[g % key] = v.get(key)
        s[g % 'battery_power'] = vt.battery_power(v) if on else None
        for key in ('yield_today', 'yield_yesterday', 'yield_total', 'max_power_today'):
            s[g % key] = last.get(key)


class SystemBus:
    """Read-only lookups on the GX's D-Bus (which Victron services exist, and their values)."""

    def __init__(self):
        import dbus
        self._bus = dbus.SystemBus()

    def list_names(self):
        return self._bus.list_names()

    def get_object(self, service, path):
        return self._bus.get_object(service, path, introspect=False)


# --------------------------------------------------------------------------- web page

class WebApp:
    def __init__(self, cfg, telemetry, history, guard, port_fn):
        self.telemetry = telemetry
        self.history = history
        self.guard = guard
        self.port_fn = port_fn
        self.name = cfg['dbus']['custom_name']
        self.service_class = cfg['dbus']['service_class']
        self.password = cfg['web']['password']
        with open(os.path.join(BASE_DIR, 'webui.html'), 'rb') as f:
            self.page = f.read()

    def status(self):
        snap = self.telemetry.snapshot()
        v = snap['values']
        last = snap['last']
        guard = self.guard.describe()
        guard['status'] = self.guard.status
        return {
            'name': self.name,
            'port': self.port_fn(),
            'driver_version': VERSION,
            'monitor_only': True,
            'service_class': self.service_class,
            'online': snap['online'],
            'values': v,
            'battery_power': vt.battery_power(v) if v else None,
            'state_text': vt.state_text(v.get('state')) if v else None,
            'error_text': vt.error_text(v.get('error')) if v else None,
            'tracker_text': vt.tracker_text(v.get('tracker_mode')) if v else None,
            'off_reasons': vt.off_reason_texts(v.get('off_reason')) if v else [],
            'yields': {k: last.get(k) for k in ('yield_today', 'yield_yesterday', 'yield_total',
                                                'max_power_today', 'max_power_yesterday')},
            'device': {'serial': last.get('serial'), 'firmware': last.get('firmware'),
                       'product_id': (None if last.get('product_id') is None
                                      else '0x%04X' % last['product_id'])},
            'guard': guard,
            'age': None if snap['age'] is None else round(snap['age'], 1),
            'blocks': snap['blocks'],
            'bad_blocks': snap['bad_blocks'],
        }

    def history_json(self, query):
        m = re.search(r'(?:^|&)minutes=(\d+)', query)
        minutes = min(int(m.group(1)), 24 * 60) if m else 60
        return {
            'interval': self.history.interval,
            'fields': History.FIELDS,
            'points': self.history.points(minutes * 60),
        }


def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        server_version = 'mppt-monitor/' + VERSION

        def log_message(self, fmt, *args):
            pass

        def _send(self, code, body, content_type, extra=()):
            self.send_response(code)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            for k, v in extra:
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code, obj, extra=()):
            self._send(code, json.dumps(obj).encode(), 'application/json', extra)

        def _authorized(self):
            if not app.password:
                return True
            header = self.headers.get('Authorization', '')
            if header.startswith('Basic '):
                try:
                    password = base64.b64decode(header[6:]).decode().partition(':')[2]
                    if hmac.compare_digest(password.encode(), app.password.encode()):
                        return True
                except ValueError:
                    pass
            self.send_response(401)
            self.send_header('WWW-Authenticate', 'Basic realm="MPPT monitor"')
            self.send_header('Content-Length', '0')
            self.end_headers()
            return False

        def do_GET(self):
            if not self._authorized():
                return
            path, _, query = self.path.partition('?')
            if path == '/':
                self._send(200, app.page, 'text/html; charset=utf-8')
            elif path == '/api/status':
                self._json(200, app.status())
            elif path == '/api/history':
                self._json(200, app.history_json(query))
            else:
                self._json(404, {'error': 'not found'})

        def _read_only(self):
            self._json(405, {'error': 'this is a monitor: it is read-only'}, (('Allow', 'GET'),))

        do_POST = do_PUT = do_PATCH = do_DELETE = _read_only

    return Handler


def start_web(cfg, app):
    bind, port = cfg['web']['bind'], cfg['web'].getint('port')
    try:
        server = ThreadingHTTPServer((bind, port), make_handler(app))
    except OSError as e:
        log.error('Web page disabled: cannot listen on %s:%d (%s)', bind, port, e)
        return
    server.daemon_threads = True
    start_thread('web', server.serve_forever)
    log.info('Web page on http://%s:%d/', bind, port)


# --------------------------------------------------------------------------- terminal tools

def venus_version():
    """Venus OS version as (major, minor, text), or None when not on a GX device."""
    try:
        with open(VERSION_FILE) as f:
            text = f.readline().strip()
    except OSError:
        return None
    m = re.match(r'v?(\d+)\.(\d+)', text)
    return (int(m.group(1)), int(m.group(2)), text) if m else (0, 0, text)


def serial_ports():
    """Candidate VE.Direct ports: the GX's built-in ones and USB serial adapters."""
    names = sorted(glob.glob(os.path.join(DEV, 'ttyS*')) + glob.glob(os.path.join(DEV, 'ttyUSB*')))
    builtin = [p for p in names if re.search(r'ttyS[5-7]$', p)]   # Cerbo GX VE.Direct ports
    return builtin + [p for p in names if 'ttyUSB' in p]


def by_id_names():
    """{'/dev/ttyUSB0': '/dev/serial/by-id/usb-VictronEnergy_...'}"""
    out = {}
    for link in glob.glob(os.path.join(DEV, 'serial', 'by-id', '*')):
        out[os.path.realpath(link)] = link
    return out


def print_frame(values, raw):
    if not values:
        return
    v = values
    def f(key, fmt):
        return '---' if v.get(key) is None else fmt % v[key]
    print('Battery %s %s %s | PV %s %s | %s | %s' % (
        f('battery_voltage', '%.2f V'), f('battery_current', '%+.1f A'),
        '---' if vt.battery_power(v) is None else '%+d W' % vt.battery_power(v),
        f('pv_voltage', '%.1f V'), f('pv_power', '%d W'),
        vt.state_text(v.get('state')) or '---', vt.error_text(v.get('error')) or '---'))
    print('Yield today %s, yesterday %s, total %s | max today %s | serial %s, firmware %s' % (
        f('yield_today', '%.2f kWh'), f('yield_yesterday', '%.2f kWh'), f('yield_total', '%.1f kWh'),
        f('max_power_today', '%d W'), v.get('serial') or '---', v.get('firmware') or '---'))


def run_scan(cfg, bus_factory=None, seconds=3.0, state_path=None):
    """Shows every VE.Direct port, who has it open, and which device Victron's driver sees there.

    Never opens a port that something else is using, and never writes to any port.
    """
    version = venus_version()
    if version:
        ok = version[:2] >= (3, 80)
        print('Venus OS %s %s' % (version[2], 'OK' if ok else
              '- the switch pane and VRM Switches need v3.80 or newer (Settings > General > Firmware)'))
    devices = []
    if bus_factory is not None:
        try:
            devices = victron_devices(bus_factory())
        except Exception as e:
            print('(Could not read D-Bus: %s)' % e)
    by_tty = {d['tty']: d for d in devices}
    ids = by_id_names()
    ports = serial_ports()
    if not ports:
        print('No VE.Direct or USB serial ports found.')
        return
    print('VE.Direct ports (nothing is ever sent to any device):')
    for path in ports:
        name = os.path.basename(path)
        label = 'built-in VE.Direct' if name.startswith('ttyS') else 'USB  ' + ids.get(path, '')
        holders = port_holders(path)
        d = by_tty.get(name)
        line = '  %-8s %s' % (name, label.strip())
        print(line)
        if d:
            print('           Victron\'s driver sees: %s %s (%s)  <- DVCC can control this' % (
                d.get('product') or '?', d.get('serial') or '', d['type']))
        if holders:
            print('           open by: %s' % ', '.join('%s (pid %d)' % (n, p) for p, n in holders))
            continue
        try:
            port = SerialPort(path)
        except OSError as e:
            print('           cannot open: %s' % e)
            continue
        parser = vt.TextParser()
        values = {}
        end = time.monotonic() + seconds
        try:
            while time.monotonic() < end:
                for block in parser.feed(port.read(0.3)):
                    values.update(vt.decode(block))
        except OSError:
            pass
        finally:
            port.close()
        if values:
            print('           free; heard an MPPT: serial %s, %s' % (
                values.get('serial') or '?', vt.state_text(values.get('state')) or '?'))
        else:
            print('           free; nothing heard')
    print()
    solar = [d for d in devices if d['type'] == 'solarcharger']
    if cfg['vedirect']['port'].strip().lower() == 'auto':
        wanted = cfg['vedirect']['serial_number'].strip()
        remembered = load_json(state_path) if state_path else {}
        path, reason = choose_port(devices, wanted, remembered)
        print('config.ini has port = auto: %s.' % reason)
    else:
        print('config.ini has port = %s.' % cfg['vedirect']['port'])
    if solar:
        print('Installing the monitor stops Victron\'s driver on the MPPT\'s port, so DVCC can no longer '
              'control it.')


def run_monitor(cfg, bus_factory, state_path):
    """Prints the MPPT's readings once a second (Cerbo Setup option 3)."""
    web_port = cfg['web'].getint('port')
    if service_running() and web_port:
        print('The monitor service is running and has the port, so this shows its readings '
              '(http://127.0.0.1:%d). Ctrl+C to stop.' % web_port)
        while True:
            try:
                with urllib.request.urlopen('http://127.0.0.1:%d/api/status' % web_port, timeout=3) as r:
                    s = json.loads(r.read().decode())
                print('--- %s  %s  %s  control: %s' % (time.strftime('%H:%M:%S'), s.get('port'),
                      'ONLINE' if s['online'] else 'NO DATA', s['guard']['status']))
                print_frame(s.get('values'), None)
            except Exception as e:
                print('Could not read the service (%s). Is a web password set?' % e)
            time.sleep(1)
    chooser = PortChooser(cfg, state_path, bus_factory)
    path = chooser.find()
    if not path:
        print('No MPPT port found. Run --scan, or set port in config.ini.')
        return
    guard = Guard(cfg['vedirect'].getboolean('take_over'))
    if guard.check(path):
        if guard.take_over:
            print('Asked Venus OS to stop its own driver on %s (restart the Cerbo to give the port '
                  'back, or install the monitor to keep it).' % path)
            time.sleep(3)
        else:
            print('Warning: %s is in use by %s, and take_over = no. The readings will be patchy.'
                  % (path, ', '.join(guard.describe()['holders'])))
    port = SerialPort(path)
    print('Reading %s read-only (nothing is ever sent to the MPPT). Ctrl+C to stop.' % path)
    telemetry = Telemetry(cfg['vedirect'].getfloat('timeout'))
    next_print = time.monotonic() + 1
    while True:
        telemetry.feed_bytes(port.read(0.5))
        if time.monotonic() >= next_print:
            next_print += 1
            snap = telemetry.snapshot()
            print('--- %s %s  %s  blocks %d, bad %d' % (path, time.strftime('%H:%M:%S'),
                  'ONLINE' if snap['online'] else 'NO DATA', snap['blocks'], snap['bad_blocks']))
            print_frame(snap['values'], snap['raw'])


def service_running():
    try:
        out = subprocess.run(['svstat', '/service/dbus-mppt-monitor'], stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, universal_newlines=True, timeout=5).stdout
        return ': up ' in out
    except (OSError, subprocess.SubprocessError):
        return False


# --------------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description='Monitor-only Victron MPPT driver for Venus OS')
    ap.add_argument('--config', default=os.path.join(BASE_DIR, 'config.ini'))
    ap.add_argument('--port', help='VE.Direct port, overrides [vedirect] port in the config')
    ap.add_argument('--scan', action='store_true', help='show the VE.Direct ports and what is on each')
    ap.add_argument('--monitor', action='store_true', help='print the MPPT readings in the terminal')
    ap.add_argument('--simulate', action='store_true',
                    help='run against a pretend MPPT; no serial port is used at all')
    ap.add_argument('--no-dbus', action='store_true', help='run without D-Bus (web page only)')
    ap.add_argument('--debug', action='store_true')
    args = ap.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO,
                        format='%(levelname)s: %(message)s')
    cfg = load_config(args.config)
    if args.port:
        cfg['vedirect']['port'] = args.port
    state_path = os.path.join(BASE_DIR, 'state.json')
    bus_factory = None if args.no_dbus else SystemBus

    if args.scan:
        run_scan(cfg, bus_factory, state_path=state_path)
        return
    if args.monitor:
        run_monitor(cfg, bus_factory, state_path)
        return

    ui_file = 'ui.json'
    if args.simulate:
        simulation_config(cfg)
        ui_file = 'ui-sim.json'
        signal.signal(signal.SIGINT, lambda *a: os._exit(0))
        signal.signal(signal.SIGTERM, lambda *a: os._exit(0))
        log.warning('SIMULATION: pretend MPPT, no serial port is used. Ctrl+C to stop.')

    log.info('MPPT monitor %s starting (monitor only: nothing is ever sent to the MPPT)', VERSION)
    telemetry = Telemetry(cfg['vedirect'].getfloat('timeout'))
    history = History(telemetry)
    guard = Guard(cfg['vedirect'].getboolean('take_over'))
    serial_wanted = cfg['vedirect']['serial_number'].strip()

    dbus_service = None
    if not args.no_dbus:
        from dbus.mainloop.glib import DBusGMainLoop
        DBusGMainLoop(set_as_default=True)
        dbus_service = DbusService(cfg, UiSettings(os.path.join(BASE_DIR, ui_file)),
                                   'mppt_monitor_sim' if args.simulate else 'mppt_monitor')

    reader = chooser = None
    if args.simulate:
        guard.path = 'simulated'
        start_thread('sim', sim_reader, SimulatedMppt(), telemetry)
        port_fn = lambda: 'simulated'
    else:
        reader = Reader(guard, telemetry, serial_wanted)
        chooser = PortChooser(cfg, state_path, bus_factory or (lambda: None))
        start_thread('vedirect', reader.run)
        port_fn = lambda: guard.path
    if cfg['web'].getint('port'):
        start_web(cfg, WebApp(cfg, telemetry, history, guard, port_fn))

    status = {'online': None, 'guard': None, 'serial_warned': None, 'next_lookup': 0.0}

    def lookup(now):
        """Every few seconds: find the port (auto), and see whether Victron's driver publishes it."""
        if now < status['next_lookup']:
            return
        status['next_lookup'] = now + GUARD_INTERVAL
        path = chooser.find()
        if path:
            reader.set_path(path)
        if bus_factory and guard.path:
            try:
                devices = victron_devices(bus_factory())
            except Exception:
                devices = []
            name = tty_name(guard.path)
            guard.set_victron_services([d['service'] for d in devices if d['tty'] == name])

    def tick():
        now = time.monotonic()
        if reader is not None:
            lookup(now)
        snap = telemetry.snapshot()
        history.tick()
        if snap['online'] != status['online']:
            if snap['online']:
                log.info('MPPT online')
            elif status['online'] is None:
                log.info('Waiting for data from the MPPT')
            else:
                log.warning('MPPT OFFLINE: no data on %s', guard.path)
            status['online'] = snap['online']
        if guard.status != status['guard']:
            status['guard'] = guard.status
            if guard.status == 'victron':
                log.warning('Victron\'s driver is active on %s: %s', guard.path, guard.describe())
            else:
                log.info('Control status: %s', guard.status)
        serial = snap['last'].get('serial')
        if reader is not None and snap['online'] and serial:
            if serial_wanted and serial != serial_wanted:
                if status['serial_warned'] != serial:
                    status['serial_warned'] = serial
                    log.error('%s has MPPT %s, not serial_number = %s', guard.path, serial, serial_wanted)
            elif cfg['vedirect']['port'].strip().lower() == 'auto' and guard.path:
                chooser.remember(guard.path, serial)
        if dbus_service is not None:
            dbus_service.refresh(snap, guard.status, guard.path)

    if dbus_service is None:
        while True:
            time.sleep(1)
            tick()

    from gi.repository import GLib

    def glib_tick():
        try:
            tick()
        except Exception:
            log.exception('Update failed')
        return True

    glib_tick()
    GLib.timeout_add(int(cfg['dbus'].getfloat('update_interval') * 1000), glib_tick)
    GLib.MainLoop().run()


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        pass
