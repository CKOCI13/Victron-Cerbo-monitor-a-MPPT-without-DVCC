"""What the monitor publishes on D-Bus (the Cerbo screen, VRM, MQTT), using the stand-in vedbus.

Also checks every switch-pane item against the rules in Victron's GUI source (gui-v2) that
decide whether an item is shown at all.
"""

import os
import shutil
import tempfile
import unittest

from support import driver, vt, load_config, MPPT_BLOCK


class DbusTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.cfg = load_config()
        self.ui = driver.UiSettings(os.path.join(self.tmp, 'ui.json'))
        self.dbus = driver.DbusService(self.cfg, self.ui)
        self.s = self.dbus.svc
        self.telemetry = driver.Telemetry(timeout=5)

    def refresh(self, status='monitor_only', port='/dev/ttyS6'):
        self.dbus.refresh(self.telemetry.snapshot(), status, port)

    def test_service_name_and_class(self):
        self.assertEqual(self.s.name, 'com.victronenergy.dcsource.mppt_monitor')
        self.assertTrue(self.s.registered)
        self.assertEqual(self.s['/Settings/MonitorMode'], -1)
        self.assertEqual(self.s['/DeviceInstance'], 71)
        self.assertEqual(self.s['/CustomName'], 'Solar MPPT')

    def test_values(self):
        self.telemetry.feed_bytes(MPPT_BLOCK)
        self.refresh()
        s = self.s
        self.assertEqual(s['/Dc/0/Voltage'], 53.12)
        self.assertEqual(s['/Dc/0/Current'], 12.3)
        self.assertEqual(s['/Dc/0/Power'], 653)
        self.assertEqual(s['/History/EnergyOut'], 1234.56)
        self.assertEqual(s['/Serial'], 'HQ2245ABCDE')
        self.assertEqual(s['/FirmwareVersion'], '1.64')
        self.assertEqual(s['/Mppt/StateText'], 'Bulk')
        self.assertEqual(s['/Mppt/ErrorText'], 'No error')
        self.assertEqual(s['/Mppt/PvPower'], 670)
        self.assertEqual(s['/Mppt/YieldToday'], 3.21)
        self.assertEqual(s['/Mppt/ControlText'], "Monitor only (DVCC can't reach it)")
        self.assertEqual(s['/Mppt/Port'], '/dev/ttyS6')

    def test_switch_pane_values(self):
        self.telemetry.feed_bytes(MPPT_BLOCK)
        self.refresh()
        g = lambda ch: self.s['/GenericInput/%s/Value' % ch]
        self.assertEqual(driver.STATE_LABELS[g('state')], 'Bulk')
        self.assertEqual(driver.ERROR_LABELS[g('error')], 'None')
        self.assertEqual(driver.GUARD_LABELS[g('control')], "Monitor only (DVCC can't reach it)")
        self.assertEqual(g('pv_power'), 670)
        self.assertEqual(g('battery_power'), 653)
        self.assertEqual(g('yield_today'), 3.21)
        self.assertEqual(self.s['/GenericInput/control/Status'], 0)

    def test_victron_driver_active_is_an_alarm(self):
        self.refresh(status='victron')
        self.assertEqual(self.s['/GenericInput/control/Status'], 1)
        self.assertEqual(self.s['/Mppt/ControlText'], 'Victron driver active!')

    def test_error_shows_and_turns_red(self):
        block = vt.encode_block((('V', '53000'), ('I', '0'), ('VPV', '200000'), ('PPV', '0'),
                                 ('CS', '2'), ('ERR', '33')))
        self.telemetry.feed_bytes(block)
        self.refresh()
        self.assertEqual(driver.ERROR_LABELS[self.s['/GenericInput/error/Value']],
                         'Error 33: PV over-voltage')
        self.assertEqual(self.s['/GenericInput/error/Status'], 1)
        self.assertEqual(driver.STATE_LABELS[self.s['/GenericInput/state/Value']], 'Fault')

    def test_unknown_codes(self):
        self.assertEqual(driver.STATE_LABELS[driver.state_index(99)], 'Unknown')
        self.assertEqual(driver.ERROR_LABELS[driver.error_index(250)], 'Other error')

    def test_offline(self):
        self.refresh(status='waiting', port=None)
        self.assertEqual(driver.STATE_LABELS[self.s['/GenericInput/state/Value']], 'No communication')
        self.assertIsNone(self.s['/Dc/0/Voltage'])
        self.assertEqual(self.s['/Mppt/Online'], 0)

    def test_values_are_read_only(self):
        for path, item in self.s.paths.items():
            if item['writeable']:
                self.assertTrue(path.endswith(('/Settings/CustomName', '/Settings/Group',
                                               '/Settings/ShowUIInput')), path)
        self.assertEqual(self.s.SetValue('/Dc/0/Voltage', 1.0), 1)
        self.assertEqual(self.s.SetValue('/Settings/MonitorMode', -3), 1)

    def test_ui_settings_are_kept(self):
        path = '/GenericInput/yield_total/Settings/ShowUIInput'
        self.assertEqual(self.s[path], 0)                      # hidden by default
        self.assertEqual(self.s.SetValue(path, 6), 0)
        self.assertEqual(self.s.SetValue(path, 99), 2)         # refused
        again = driver.DbusService(load_config(), driver.UiSettings(self.ui.path))
        self.assertEqual(again.svc[path], 6)

    def test_gui_v2_rules_for_every_item(self):
        # gui-v2 shows a GenericInput only if /Name isn't empty, /Settings/Type is valid and set
        # in /Settings/ValidTypes, and a value item has a /Settings/Unit.
        for channel, name, typ, unit, decimals, labels, card in driver.INPUTS:
            base = '/GenericInput/' + channel
            self.assertTrue(self.s[base + '/Name'])
            self.assertIn(self.s[base + '/Settings/Type'], (0, 1))
            self.assertTrue(self.s[base + '/Settings/ValidTypes'] & (1 << typ))
            if typ == driver.INPUT_VALUE:
                self.assertTrue(self.s[base + '/Settings/Unit'])
            else:
                self.assertTrue(self.s[base + '/Settings/Labels'])
            group = self.s[base + '/Settings/Group']
            if card:
                self.assertEqual(group, driver.CARDS[card] % 'Solar MPPT')
                self.assertLessEqual(len(group.encode()), 32)
            else:
                self.assertIsNone(group)

    def test_other_class(self):
        cfg = load_config()
        cfg['dbus']['service_class'] = 'mpptmonitor'
        d = driver.DbusService(cfg, driver.UiSettings(os.path.join(self.tmp, 'x.json')), 'x')
        self.assertEqual(d.svc.name, 'com.victronenergy.mpptmonitor.x')
        self.assertNotIn('/Settings/MonitorMode', d.svc.paths)


if __name__ == '__main__':
    unittest.main()
