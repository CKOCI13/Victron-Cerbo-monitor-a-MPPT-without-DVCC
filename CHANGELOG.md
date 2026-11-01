# Changelog

## 1.0.0 (2026-11-01)

First version.

- Reads a Victron MPPT solar charger over VE.Direct (text protocol), with the port opened read-only.
- Stops Victron's own VE.Direct driver on the MPPT's port only (`stop-tty.sh`), checks every 10 seconds that it
  stays off, and shows the result as **Control** on the Cerbo screen and the web page.
- Publishes the MPPT as a **DC source**, which DVCC never controls; classes DVCC controls are refused.
- Cerbo switch pane and VRM Switches: Control, State, Error, yields, max power, panel and battery cards.
- Device list and Overview: shown as a DC source named "Solar MPPT".
- Web page on port 8090 with live values, error explanations and 24-hour charts, for a tablet.
- `port = auto` finds the MPPT through Victron's driver and remembers it; `serial_number` picks one of several.
- `--scan`, `--monitor` and a pretend MPPT (`--simulate`); Windows helper `Cerbo Setup.bat`; USB-stick installer.
