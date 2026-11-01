# Tests

Run them on any PC with Python 3 (Linux or Mac for all of them; on Windows the pseudo-terminal and shell-script
tests are skipped):

```
python -m unittest discover -s tests -v
```

| File | Checks |
|---|---|
| `test_vedirect_text.py` | The VE.Direct parser: checksums (one worked out by hand), HEX frames, cut-off blocks, noise, units and texts |
| `test_monitor_only.py` | The code can't write to the port or register as a class DVCC controls |
| `test_port.py` | Finding the MPPT's port, spotting Victron's driver in a pretend `/proc`, calling `stop-tty.sh`, the scan |
| `test_reader.py` | End to end on a pseudo-terminal: read-only open, reader thread, decoding |
| `test_dbus.py` | D-Bus paths and switch-pane items, checked against gui-v2's display rules |
| `test_web_and_history.py` | Web API, read-only HTTP, password, history |
| `test_simulation.py` | The pretend MPPT |
| `test_install_script.py`, `test_usb_installer.py` | `install.sh` and the USB-stick installer |

`fakes/vedbus.py` stands in for Victron's `vedbus` module, so the D-Bus code runs without a D-Bus.
