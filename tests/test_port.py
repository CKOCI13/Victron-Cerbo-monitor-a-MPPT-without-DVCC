"""Finding the MPPT's port, and keeping Victron's VE.Direct driver off it.

/proc and stop-tty.sh are replaced by a temporary folder and a small script, so these run on any
Linux or Mac PC.
"""

import os
import shutil
import stat
import tempfile
import unittest

from support import driver


class FakeItem:
    def __init__(self, value):
        self.value = value

    def GetValue(self, dbus_interface=None):
        if isinstance(self.value, Exception):
            raise self.value
        return self.value


class FakeBus:
    """Read-only D-Bus stand-in: {service: {path: value}}."""

    def __init__(self, services):
        self.services = services

    def list_names(self):
        return list(self.services) + [':1.23', 'org.freedesktop.DBus']

    def get_object(self, service, path):
        return FakeItem(self.services[service].get(path, []))


def mppt(tty, serial, product='SmartSolar MPPT 150/35'):
    return {'com.victronenergy.solarcharger.' + tty: {'/ProductName': product, '/Serial': serial}}


class VictronDevicesTest(unittest.TestCase):
    def test_lists_vedirect_services_only(self):
        bus = FakeBus(dict(mppt('ttyS6', 'HQ1'), **{
            'com.victronenergy.battery.socketcan_can0': {'/ProductName': 'Lithionics'},
            'com.victronenergy.solarcharger.socketcan_can1_vi0_uc123': {},
            'com.victronenergy.dcsource.mppt_monitor': {},
            'com.victronenergy.battery.ttyUSB0': {'/ProductName': 'SmartShunt', '/Serial': []},
        }))
        devices = sorted(driver.victron_devices(bus), key=lambda d: d['tty'])
        self.assertEqual([(d['type'], d['tty'], d['serial']) for d in devices],
                         [('solarcharger', 'ttyS6', 'HQ1'), ('battery', 'ttyUSB0', None)])
        self.assertEqual(devices[0]['product'], 'SmartSolar MPPT 150/35')

    def test_a_failing_lookup_gives_none(self):
        bus = FakeBus({'com.victronenergy.solarcharger.ttyS5': {'/Serial': RuntimeError('gone')}})
        self.assertIsNone(driver.victron_devices(bus)[0]['serial'])


class ChoosePortTest(unittest.TestCase):
    def devices(self, *pairs):
        bus = FakeBus({k: v for tty, serial in pairs for k, v in mppt(tty, serial).items()})
        return driver.victron_devices(bus)

    def test_one_mppt(self):
        path, why = driver.choose_port(self.devices(('ttyS6', 'HQ1')), '', {})
        self.assertEqual(path, '/dev/ttyS6')
        self.assertIn('ttyS6', why)

    def test_two_mppts_need_a_serial_number(self):
        devs = self.devices(('ttyS5', 'HQ1'), ('ttyS6', 'HQ2'))
        path, why = driver.choose_port(devs, '', {})
        self.assertIsNone(path)
        self.assertIn('serial_number', why)
        self.assertEqual(driver.choose_port(devs, 'HQ2', {})[0], '/dev/ttyS6')

    def test_other_device_types_are_not_taken(self):
        bus = FakeBus({'com.victronenergy.battery.ttyS5': {'/Serial': 'HQ9'}})
        self.assertIsNone(driver.choose_port(driver.victron_devices(bus), '', {})[0])

    def test_remembered_port_when_victrons_driver_is_already_stopped(self):
        remembered = {'port': '/dev/ttyS7', 'serial': 'HQ1'}
        exists = lambda p: p == '/dev/ttyS7'
        self.assertEqual(driver.choose_port([], '', remembered, exists)[0], '/dev/ttyS7')
        self.assertEqual(driver.choose_port([], 'HQ1', remembered, exists)[0], '/dev/ttyS7')
        self.assertIsNone(driver.choose_port([], 'HQ2', remembered, exists)[0])
        self.assertIsNone(driver.choose_port([], '', remembered, lambda p: False)[0])

    def test_a_moved_mppt_wins_over_the_remembered_port(self):
        remembered = {'port': '/dev/ttyS7', 'serial': 'HQ1'}
        path, _ = driver.choose_port(self.devices(('ttyS5', 'HQ1')), '', remembered, lambda p: True)
        self.assertEqual(path, '/dev/ttyS5')

    def test_fixed_port(self):
        cfg = driver.load_config('/nonexistent')
        cfg['vedirect']['port'] = 'ttyS6'
        chooser = driver.PortChooser(cfg, '/nonexistent/state.json', lambda: None)
        self.assertEqual(chooser.find(), '/dev/ttyS6')
        cfg['vedirect']['port'] = '/dev/serial/by-id/usb-VictronEnergy_VE_Direct_cable_X-if00-port0'
        self.assertEqual(chooser.find(), cfg['vedirect']['port'])


class FakeSystem(unittest.TestCase):
    """A temporary /proc with pretend processes, a pretend /dev, and a stop-tty.sh that logs."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.proc = os.path.join(self.tmp, 'proc')
        os.mkdir(self.proc)
        self.dev = os.path.join(self.tmp, 'dev')
        os.mkdir(self.dev)
        self.port = os.path.join(self.dev, 'ttyS6')
        open(self.port, 'w').close()
        self.calls = os.path.join(self.tmp, 'calls')
        self.stop = os.path.join(self.tmp, 'stop-tty.sh')
        with open(self.stop, 'w') as f:
            f.write('#!/bin/sh\necho "$1" >> %s\n' % self.calls)
        os.chmod(self.stop, os.stat(self.stop).st_mode | stat.S_IXUSR)
        for name, value in (('PROC', self.proc), ('DEV', self.dev), ('STOP_TTY', self.stop)):
            self.addCleanup(setattr, driver, name, getattr(driver, name))
            setattr(driver, name, value)

    def add_process(self, pid, argv, files):
        d = os.path.join(self.proc, str(pid))
        os.makedirs(os.path.join(d, 'fd'))
        with open(os.path.join(d, 'cmdline'), 'wb') as f:
            f.write(b'\0'.join(a.encode() for a in argv) + b'\0')
        for n, target in enumerate(files):
            os.symlink(target, os.path.join(d, 'fd', str(n)))
        return d

    def stop_calls(self):
        try:
            with open(self.calls) as f:
                return f.read().split()
        except FileNotFoundError:
            return []


class HoldersTest(FakeSystem):
    def test_finds_victrons_driver(self):
        self.add_process(100, ['/opt/victronenergy/vedirect-interface/vedirect-dbus', '-s', 'ttyS6'],
                         ['/dev/null', self.port])
        self.add_process(101, ['python3', '/data/other/thing.py'], [self.port])
        self.add_process(102, ['sleep'], ['/dev/null'])
        self.assertEqual(sorted(driver.port_holders(self.port, exclude_pid=1)),
                         [(100, 'vedirect-dbus'), (101, 'thing.py')])

    def test_own_process_is_not_counted(self):
        self.add_process(100, ['vedirect-dbus'], [self.port])
        self.assertEqual(driver.port_holders(self.port, exclude_pid=100), [])

    def test_follows_by_id_links(self):
        by_id = os.path.join(self.dev, 'by-id-cable')
        os.symlink(self.port, by_id)
        self.add_process(100, ['vedirect-dbus'], [self.port])
        self.assertEqual(driver.port_holders(by_id, exclude_pid=1), [(100, 'vedirect-dbus')])
        self.assertEqual(driver.tty_name(by_id), 'ttyS6')


class GuardTest(FakeSystem):
    def test_stops_victrons_driver_and_rate_limits(self):
        self.add_process(100, ['vedirect-dbus'], [self.port])
        guard = driver.Guard(take_over=True)
        with self.assertLogs('mppt-monitor', 'WARNING'):
            guard.check(self.port, now=1000.0)
        guard.check(self.port, now=1001.0)                    # too soon: not asked again
        guard.check(self.port, now=1000.0 + driver.GUARD_INTERVAL)
        self.assertEqual(self.stop_calls(), ['ttyS6', 'ttyS6'])
        self.assertEqual(guard.status, 'victron')

    def test_monitor_only_when_alone(self):
        guard = driver.Guard(take_over=True)
        self.assertEqual(guard.status, 'waiting')
        guard.check(self.port)
        self.assertEqual(guard.status, 'monitor_only')
        self.assertEqual(self.stop_calls(), [])

    def test_victron_service_on_dbus_counts_as_active(self):
        guard = driver.Guard(take_over=True)
        guard.check(self.port)
        guard.set_victron_services(['com.victronenergy.solarcharger.ttyS6'])
        self.assertEqual(guard.status, 'victron')
        guard.set_victron_services([])
        self.assertEqual(guard.status, 'monitor_only')

    def test_take_over_no_never_stops_anything(self):
        self.add_process(100, ['vedirect-dbus'], [self.port])
        guard = driver.Guard(take_over=False)
        guard.check(self.port)
        self.assertEqual(self.stop_calls(), [])
        self.assertEqual(guard.status, 'victron')

    def test_missing_stop_tty(self):
        os.unlink(self.stop)
        self.add_process(100, ['vedirect-dbus'], [self.port])
        with self.assertLogs('mppt-monitor', 'WARNING') as logs:
            driver.Guard(take_over=True).check(self.port)
        self.assertTrue(any('not found' in m for m in logs.output))


class ScanTest(FakeSystem):
    def test_scan_names_the_ports_without_touching_busy_ones(self):
        import contextlib
        import io
        for n in (5, 7):
            open(os.path.join(self.dev, 'ttyS%d' % n), 'w').close()
        self.add_process(100, ['vedirect-dbus'], [self.port])
        cfg = driver.load_config('/nonexistent')
        bus = FakeBus(mppt('ttyS6', 'HQ1'))
        opened = []
        real = driver.SerialPort
        self.addCleanup(setattr, driver, 'SerialPort', real)

        def refuse(path):
            opened.append(path)
            raise OSError('not a tty')
        driver.SerialPort = refuse
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            driver.run_scan(cfg, lambda: bus, seconds=0.1)
        text = out.getvalue()
        self.assertIn('ttyS6', text)
        self.assertIn('SmartSolar MPPT 150/35 HQ1', text)
        self.assertIn('open by: vedirect-dbus', text)
        self.assertIn('port = auto: found', text)
        self.assertNotIn(self.port, opened)    # the busy port is never opened
        self.assertEqual(sorted(os.path.basename(p) for p in opened), ['ttyS5', 'ttyS7'])


if __name__ == '__main__':
    unittest.main()
