"""The monitor must never be able to send anything to the MPPT, or let DVCC control it.

These tests look at the code itself, so that a later change can't quietly add a way to write.
"""

import ast
import os
import unittest

from support import driver, DRIVER_DIR


def driver_trees():
    for fname in sorted(os.listdir(DRIVER_DIR)):
        if fname.endswith('.py'):
            with open(os.path.join(DRIVER_DIR, fname)) as f:
                yield fname, ast.parse(f.read())


class MonitorOnlyTest(unittest.TestCase):
    def test_serial_port_class_cannot_write(self):
        for name in ('write', 'send', 'send_hex', 'ping'):
            self.assertFalse(hasattr(driver.SerialPort, name), name)

    def test_port_is_opened_read_only(self):
        # The only os.open() in the driver folder opens with O_RDONLY, and nothing asks for
        # write access, so the kernel itself refuses any write to the port.
        opens = []
        for fname, tree in driver_trees():
            for node in ast.walk(tree):
                if isinstance(node, ast.Attribute) and node.attr in ('O_WRONLY', 'O_RDWR'):
                    self.fail('%s line %d uses os.%s' % (fname, node.lineno, node.attr))
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and node.func.attr == 'open' and isinstance(node.func.value, ast.Name)
                        and node.func.value.id == 'os'):
                    opens.append((fname, node))
        self.assertEqual(len(opens), 1)
        flags = ast.dump(opens[0][1].args[1])
        self.assertIn('O_RDONLY', flags)

    def test_no_write_calls_anywhere_in_the_driver_folder(self):
        # Allowed: the web server's HTTP response (self.wfile.write and the send_* helpers).
        allowed = {'send_response', 'send_header', '_send'}
        for fname, tree in driver_trees():
            for node in ast.walk(tree):
                if isinstance(node, ast.Attribute) and node.attr.startswith(('send', 'write', '_send')):
                    if node.attr in allowed or (node.attr == 'write' and _is_wfile(node)):
                        continue
                    self.fail('%s line %d uses .%s' % (fname, node.lineno, node.attr))

    def test_never_registers_as_a_class_dvcc_controls(self):
        # From dbus-systemcalc-py delegates/dvcc.py: the service types DVCC adds as chargers.
        for cls in ('solarcharger', 'alternator', 'dcgenset', 'inverter', 'multi', 'vebus'):
            self.assertIn(cls, driver.DVCC_CLASSES)

    def test_config_refuses_dvcc_classes(self):
        import tempfile
        for cls in ('solarcharger', 'SolarCharger', 'alternator', 'bad name!'):
            with tempfile.NamedTemporaryFile('w', suffix='.ini', delete=False) as f:
                f.write('[dbus]\nservice_class = %s\n' % cls)
            try:
                with self.assertLogs('mppt-monitor', 'ERROR'):
                    cfg = driver.load_config(f.name)
                self.assertEqual(cfg['dbus']['service_class'], 'dcsource')
            finally:
                os.unlink(f.name)

    def test_shipped_config_is_a_dc_source(self):
        from support import load_config
        self.assertEqual(load_config()['dbus']['service_class'], 'dcsource')

    def test_no_link_paths(self):
        # /Link/* is where DVCC writes charge voltage and current; the monitor has none.
        for fname, tree in driver_trees():
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    self.assertFalse(node.value.startswith('/Link'), '%s: %s' % (fname, node.value))


def _is_wfile(node):
    """self.wfile.write(...): the HTTP response body."""
    return isinstance(node.value, ast.Attribute) and node.value.attr == 'wfile'


if __name__ == '__main__':
    unittest.main()
