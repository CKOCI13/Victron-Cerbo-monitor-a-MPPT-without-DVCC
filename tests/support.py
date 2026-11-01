"""Shared set-up for the tests: loads the monitor so it can be tested on any PC.

The monitor normally runs on a Victron GX device. Two things make it testable elsewhere:

* Its file name contains dashes (``dbus-mppt-monitor.py``, the naming style Victron uses for
  drivers), so it can't be imported with a plain ``import``. It is loaded by path instead,
  as ``support.driver``.
* It uses Victron's ``vedbus`` library, which needs the Linux D-Bus. ``tests/fakes/vedbus.py``
  is a small stand-in with the same behaviour, so the D-Bus parts can be tested too.
"""

import importlib.util
import os
import socket
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
DRIVER_DIR = os.path.join(REPO, 'dbus-mppt-monitor')
CONFIG = os.path.join(DRIVER_DIR, 'config.ini')

sys.path.insert(0, DRIVER_DIR)                      # for "import vedirect_text"
sys.path.insert(0, os.path.join(HERE, 'fakes'))     # the stand-in "vedbus" wins over any real one

_spec = importlib.util.spec_from_file_location(
    'dbus_mppt_monitor', os.path.join(DRIVER_DIR, 'dbus-mppt-monitor.py'))
driver = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(driver)

import vedirect_text as vt  # noqa: E402  (needs the path set above)

# A block as a SmartSolar MPPT sends it: 53.12 V, 12.3 A into the battery, 98.76 V / 670 W
# from the panels, Bulk, MPP tracking, no error, 1234.56 kWh in total, 3.21 kWh today.
MPPT_FIELDS = (
    ('PID', '0xA060'), ('FW', '164'), ('SER#', 'HQ2245ABCDE'),
    ('V', '53120'), ('I', '12300'), ('VPV', '98760'), ('PPV', '670'),
    ('CS', '3'), ('MPPT', '2'), ('OR', '0x00000000'), ('ERR', '0'),
    ('LOAD', 'OFF'), ('IL', '0'),
    ('H19', '123456'), ('H20', '321'), ('H21', '812'), ('H22', '412'), ('H23', '905'),
    ('HSDS', '17'),
)
MPPT_BLOCK = vt.encode_block(MPPT_FIELDS)


def load_config():
    """The shipped config.ini, as the monitor reads it."""
    return driver.load_config(CONFIG)


def free_port():
    """A TCP port nobody is using, for starting the web page in a test."""
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]
