"""The pretend MPPT (--simulate): real VE.Direct blocks, and every charge state shows up."""

import unittest

from support import driver, vt


class SimulationTest(unittest.TestCase):
    def test_blocks_go_through_the_real_parser(self):
        sim = driver.SimulatedMppt(now=0.0)
        parser = vt.TextParser()
        for n in range(1, 30):
            sim.step(float(n))
            blocks = parser.feed(b':A0102000543\n' + sim.block())
            self.assertEqual(len(blocks), 1)
        self.assertEqual(parser.bad_blocks, 0)
        v = vt.decode(blocks[0])
        self.assertEqual(v['serial'], 'HQSIMULATED')
        self.assertGreater(v['pv_power'], 0)

    def test_a_pretend_day_shows_every_state(self):
        sim = driver.SimulatedMppt(now=0.0)
        seen = set()
        yields = []
        for n in range(1, int(sim.DAY + sim.NIGHT) * 2):
            sim.step(float(n))
            seen.add(sim.state)
            yields.append(sim.yield_today)
            self.assertTrue(50.0 <= sim.v <= 57.0)
        self.assertTrue({0, 3, 4, 5} <= seen, seen)
        self.assertGreater(max(yields), 0.05)
        self.assertGreater(sim.yield_yesterday, 0.05)   # the day rolled over

    def test_simulation_config_does_not_clash(self):
        cfg = driver.load_config('/nonexistent')
        driver.simulation_config(cfg)
        self.assertEqual(cfg['dbus']['device_instance'], '72')
        self.assertEqual(cfg['web']['port'], '8091')
        self.assertTrue(cfg['dbus']['custom_name'].endswith('(SIMULATED)'))


if __name__ == '__main__':
    unittest.main()
