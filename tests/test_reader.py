"""End to end: a pretend MPPT on a pseudo-terminal, read by the monitor's real Reader thread.

A pseudo-terminal (pty) behaves like a serial port, so this exercises SerialPort (read-only
open, 19200 8N1 set-up, select/read) and the parser together.
"""

import os
import time
import unittest

from support import driver, MPPT_BLOCK

try:
    import pty
except ImportError:   # Windows
    pty = None


@unittest.skipIf(pty is None, 'needs a pseudo-terminal (Linux or Mac)')
class ReaderTest(unittest.TestCase):
    def setUp(self):
        self.master, slave = os.openpty()
        self.path = os.ttyname(slave)
        os.close(slave)
        self.addCleanup(os.close, self.master)

    def wait_for(self, predicate, seconds=5.0):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if predicate():
                return True
            time.sleep(0.05)
        return False

    def test_serial_port_is_read_only(self):
        port = driver.SerialPort(self.path)
        self.addCleanup(port.close)
        with self.assertRaises(OSError):
            os.write(port.fd, b'x')   # the kernel refuses: the port was opened O_RDONLY
        os.write(self.master, MPPT_BLOCK)
        data = b''
        end = time.monotonic() + 3
        while len(data) < len(MPPT_BLOCK) and time.monotonic() < end:
            data += port.read(0.2)
        self.assertEqual(data, MPPT_BLOCK)

    def test_reader_thread_decodes_the_mppt(self):
        telemetry = driver.Telemetry(timeout=5)
        guard = driver.Guard(take_over=True)
        reader = driver.Reader(guard, telemetry)
        driver.start_thread('test-reader', reader.run)
        reader.set_path(self.path)
        self.assertTrue(self.wait_for(lambda: reader.port is not None), 'port never opened')
        # the MPPT sends a block a second; send a few, starting mid-block like a real port
        os.write(self.master, MPPT_BLOCK[25:] + MPPT_BLOCK + MPPT_BLOCK)
        self.assertTrue(self.wait_for(lambda: telemetry.blocks >= 2), 'no blocks decoded')
        snap = telemetry.snapshot()
        self.assertTrue(snap['online'])
        self.assertEqual(snap['values']['serial'], 'HQ2245ABCDE')
        self.assertEqual(snap['values']['battery_voltage'], 53.12)
        self.assertEqual(guard.status, 'monitor_only')
        self.assertEqual(guard.path, self.path)


if __name__ == '__main__':
    unittest.main()
