"""The web page, its API, and the 24-hour history."""

import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from support import driver, load_config, free_port, MPPT_BLOCK


class HistoryTest(unittest.TestCase):
    def test_averages_and_gaps(self):
        t = driver.Telemetry(timeout=5)
        h = driver.History(t, interval=10)
        t.feed_bytes(MPPT_BLOCK)
        t.feed_bytes(MPPT_BLOCK)
        h.add(t.take_averages(), wall_time=1000)
        h.add(t.take_averages(), wall_time=1010)   # nothing arrived: a gap
        points = h.points()
        self.assertEqual(points[0], (1000, 670, 98.76, 53.12, 12.3, 653))
        self.assertEqual(points[1], (1010, None, None, None, None, None))
        self.assertEqual(len(h.points(seconds=5)), 1)

    def test_tick_adds_one_point_per_interval(self):
        t = driver.Telemetry(timeout=5)
        h = driver.History(t, interval=10)
        start = h._next
        h.tick(start - 1)
        h.tick(start)
        h.tick(start + 1)
        self.assertEqual(len(h.points()), 1)


class WebTest(unittest.TestCase):
    def setUp(self):
        self.cfg = load_config()
        self.telemetry = driver.Telemetry(timeout=5)
        self.telemetry.feed_bytes(MPPT_BLOCK)
        self.guard = driver.Guard(take_over=True)
        self.guard.path = '/dev/ttyS6'
        self.app = driver.WebApp(self.cfg, self.telemetry, driver.History(self.telemetry),
                                 self.guard, lambda: '/dev/ttyS6')
        self.start()

    def start(self):
        self.port = free_port()
        self.server = ThreadingHTTPServer(('127.0.0.1', self.port), driver.make_handler(self.app))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def get(self, path, headers=None):
        req = urllib.request.Request('http://127.0.0.1:%d%s' % (self.port, path), headers=headers or {})
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read()

    def test_page(self):
        status, body = self.get('/')
        self.assertEqual(status, 200)
        self.assertIn(b'monitor only', body)

    def test_status(self):
        s = json.loads(self.get('/api/status')[1])
        self.assertTrue(s['online'])
        self.assertTrue(s['monitor_only'])
        self.assertEqual(s['service_class'], 'dcsource')
        self.assertEqual(s['values']['pv_power'], 670)
        self.assertEqual(s['battery_power'], 653)
        self.assertEqual(s['state_text'], 'Bulk')
        self.assertEqual(s['yields']['yield_total'], 1234.56)
        self.assertEqual(s['device'], {'serial': 'HQ2245ABCDE', 'firmware': '1.64', 'product_id': '0xA060'})
        self.assertEqual(s['guard']['status'], 'monitor_only')

    def test_history(self):
        h = json.loads(self.get('/api/history?minutes=10')[1])
        self.assertEqual(h['fields'], list(driver.History.FIELDS))

    def test_everything_but_get_is_refused(self):
        for method in ('POST', 'PUT', 'DELETE', 'PATCH'):
            req = urllib.request.Request('http://127.0.0.1:%d/api/status' % self.port, method=method,
                                         data=b'{}')
            with self.assertRaises(urllib.error.HTTPError) as e:
                urllib.request.urlopen(req, timeout=5)
            self.assertEqual(e.exception.code, 405)

    def test_password(self):
        self.app.password = 'secret'
        with self.assertRaises(urllib.error.HTTPError) as e:
            self.get('/api/status')
        self.assertEqual(e.exception.code, 401)
        import base64
        auth = 'Basic ' + base64.b64encode(b'any:secret').decode()
        self.assertEqual(self.get('/api/status', {'Authorization': auth})[0], 200)


if __name__ == '__main__':
    unittest.main()
