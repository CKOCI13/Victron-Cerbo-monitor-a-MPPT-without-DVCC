"""port = auto with a one-way cable: Victron's driver can't identify the MPPT, so the monitor
listens for it itself. Serial ports, serial-starter and /proc are replaced by stand-ins."""

import json
import os
import tempfile
import unittest

from support import driver, vt, MPPT_BLOCK, MPPT_FIELDS

BMV_BLOCK = vt.encode_block((('PID', '0x203'), ('V', '13380'), ('I', '0'), ('SOC', '1000')))


class FakePort:
    """Plays back bytes once, then stays silent. Has no write method, like SerialPort."""

    def __init__(self, data):
        self.data = data
        self.closed = False

    def read(self, timeout=0.3):
        data, self.data = self.data, b''
        return data

    def close(self):
        self.closed = True


class Cerbo:
    """Three VE.Direct ports; serial-starter keeps the unidentified ones open while probing."""

    def __init__(self, traffic, probing=('ttyS5', 'ttyS6', 'ttyS7')):
        self.traffic = traffic                      # {'ttyS6': bytes}
        self.probing = set(probing)
        self.calls = []

    def ports(self):
        return ['/dev/ttyS5', '/dev/ttyS6', '/dev/ttyS7']

    def holders(self, path):
        return [(42, 'vedirect-dbus')] if os.path.basename(path) in self.probing else []

    def stop(self, name):
        self.calls.append(('stop', name))
        self.probing.discard(name)
        return True

    def start(self, name):
        self.calls.append(('start', name))
        self.probing.add(name)
        return True

    def listen(self, path):
        return driver.listen_for_mppt(
            path, seconds=0.2,
            open_port=lambda p: FakePort(self.traffic.get(os.path.basename(p), b'')))

    def listener(self, serial_wanted='', take_over=True):
        return driver.Listener(take_over, serial_wanted, ports_fn=self.ports, listen=self.listen,
                               stop=self.stop, start=self.start, holders=self.holders,
                               sleep=lambda s: None)


class ListenForMpptTest(unittest.TestCase):
    def test_hears_an_mppt(self):
        values = driver.listen_for_mppt('x', 0.2, open_port=lambda p: FakePort(MPPT_BLOCK))
        self.assertEqual(values['serial'], dict(MPPT_FIELDS)['SER#'])

    def test_a_battery_monitor_is_not_an_mppt(self):
        self.assertIsNone(driver.listen_for_mppt('x', 0.2, open_port=lambda p: FakePort(BMV_BLOCK)))

    def test_silence(self):
        self.assertIsNone(driver.listen_for_mppt('x', 0.2, open_port=lambda p: FakePort(b'')))

    def test_a_port_that_cannot_open(self):
        def fail(p):
            raise OSError('busy')
        self.assertIsNone(driver.listen_for_mppt('x', 0.2, open_port=fail))


class ListenerTest(unittest.TestCase):
    def test_finds_the_mppt_and_hands_back_the_empty_ports(self):
        cerbo = Cerbo({'ttyS6': MPPT_BLOCK})
        found = cerbo.listener().scan(set())
        self.assertEqual(found, ('/dev/ttyS6', dict(MPPT_FIELDS)['SER#']))
        # ttyS5 was paused, heard nothing, and was handed straight back; ttyS6 stays ours.
        self.assertEqual(cerbo.calls, [('stop', 'ttyS5'), ('start', 'ttyS5'), ('stop', 'ttyS6')])

    def test_never_touches_a_port_victron_identified(self):
        cerbo = Cerbo({'ttyS5': MPPT_BLOCK, 'ttyS6': MPPT_BLOCK})
        found = cerbo.listener().scan({'ttyS5'})
        self.assertEqual(found[0], '/dev/ttyS6')
        self.assertNotIn(('stop', 'ttyS5'), cerbo.calls)

    def test_nothing_heard_gives_every_port_back(self):
        cerbo = Cerbo({'ttyS7': BMV_BLOCK})
        self.assertIsNone(cerbo.listener().scan(set()))
        self.assertEqual(cerbo.probing, {'ttyS5', 'ttyS6', 'ttyS7'})

    def test_serial_number_picks_the_right_mppt(self):
        cerbo = Cerbo({'ttyS5': MPPT_BLOCK, 'ttyS6': MPPT_BLOCK})
        self.assertIsNone(cerbo.listener('HQOTHER').scan(set()))
        self.assertEqual(cerbo.listener(dict(MPPT_FIELDS)['SER#']).scan(set())[0], '/dev/ttyS5')

    def test_take_over_no_leaves_busy_ports_alone(self):
        cerbo = Cerbo({'ttyS6': MPPT_BLOCK})
        self.assertIsNone(cerbo.listener(take_over=False).scan(set()))
        self.assertEqual(cerbo.calls, [])

    def test_a_port_that_stays_busy_is_handed_back(self):
        cerbo = Cerbo({'ttyS6': MPPT_BLOCK}, probing=('ttyS6',))
        cerbo.stop = lambda name: cerbo.calls.append(('stop', name)) or True   # doesn't free it
        self.assertIsNone(cerbo.listener().scan(set()))
        self.assertEqual(cerbo.calls, [('stop', 'ttyS6'), ('start', 'ttyS6')])


class ChooserListensTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.state = os.path.join(self.dir.name, 'state.json')
        self.clock = [1000.0]
        self.cfg = driver.load_config('/nonexistent')

    def tearDown(self):
        self.dir.cleanup()

    def chooser(self, listener):
        return driver.PortChooser(self.cfg, self.state, lambda: None, listener=listener,
                                  now=lambda: self.clock[0])

    def test_waits_for_victron_first_then_listens_and_remembers(self):
        listener = Cerbo({'ttyS6': MPPT_BLOCK}).listener()
        rounds = []
        listener.begin = lambda ttys: rounds.append(set(ttys)) or listener.run_round(set(ttys))
        chooser = self.chooser(listener)
        self.assertIsNone(chooser.find())                 # grace period: no listening yet
        self.assertEqual(rounds, [])
        self.clock[0] += driver.LISTEN_GRACE
        self.assertIsNone(chooser.find())                 # starts a round (in a thread on the GX)
        self.assertEqual(len(rounds), 1)
        self.assertEqual(chooser.find(), '/dev/ttyS6')    # the round's result
        with open(self.state) as f:
            self.assertEqual(json.load(f)['port'], '/dev/ttyS6')

    def test_rounds_are_spaced_out(self):
        listener = Cerbo({}).listener()
        rounds = []
        listener.begin = lambda ttys: rounds.append(1) or listener.run_round(set(ttys))
        chooser = self.chooser(listener)
        self.clock[0] += driver.LISTEN_GRACE
        chooser.find()
        chooser.find()
        self.assertEqual(len(rounds), 1)
        self.clock[0] += driver.LISTEN_RETRY
        chooser.find()
        self.assertEqual(len(rounds), 2)


class NoDataIsNotAFaultTest(unittest.TestCase):
    def test_state_row_says_no_communication_without_a_fault_status(self):
        from support import load_config
        cfg = load_config()
        ui = tempfile.NamedTemporaryFile(suffix='.json', delete=False)
        ui.close()
        try:
            svc = driver.DbusService(cfg, driver.UiSettings(ui.name))
            telemetry = driver.Telemetry(5.0)
            svc.refresh(telemetry.snapshot(), 'waiting', None)
            s = svc.svc
            self.assertEqual(driver.STATE_LABELS[s['/GenericInput/state/Value']], 'No communication')
            self.assertEqual(s['/GenericInput/state/Status'], 0)
        finally:
            os.unlink(ui.name)


if __name__ == '__main__':
    unittest.main()
