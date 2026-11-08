# Changelog

## 1.0.2 (2026-11-07)

Fixes `port = auto` with the **one-way cable**, found in the first real test (SmartSolar on a Cerbo GX MK2).

- `port = auto` now also finds an MPPT that Victron's driver can't identify. Victron's driver has to talk to a
  device to identify it, which a one-way cable stops, so the monitor stayed on "Waiting for the MPPT" for good.
  Now, 30 s after starting, it listens (read-only) on each VE.Direct port where Victron's driver found nothing,
  takes the one where it hears an MPPT (matching `serial_number` if set), and remembers it. Ports where Victron's
  driver found a device are never touched; ports with no MPPT are handed straight back to Venus OS. It tries
  again every 5 minutes until it hears the MPPT. Nothing is ever sent to any device.
- While there's no port yet, the **Control** row says what the last listening round found: *Listening for the
  MPPT*, *Not heard: check cable*, *Garbled data: check cable*, *No MPPT on VE.Direct* or *VE.Direct ports busy*.
  The web page lists the result for each port (bytes heard, busy, and so on), so a cable problem can be found
  from the tablet without SSH.
- With no data, the State row said **Fault**, which looked like an MPPT fault. It now says **No communication**.
- `--scan` (Cerbo Setup option 2) explains a port that Victron's driver holds but hasn't identified, instead of
  just listing it as open.

## 1.0.1 (2026-11-06)

Corrected for how the MPPT is really wired in a Grand Design LTV1: it charges the **12 V AGM chassis batteries**
from the rear 12 V bus bar, and the Nomadic D1 boosts the surplus to the 51 V Lithionics. No change to how the
monitor reads or publishes the MPPT.

- MPPT setup (README and guide): battery voltage **12 V** (was 48 V), AGM charge voltages from the battery maker
  or VictronConnect's AGM preset, and lead-acid temperature compensation left on (replaces the lithium
  cold-charging note).
- New: where the MPPT sits in an LTV1, matching the D1's boost start voltage to the MPPT's absorption and float,
  and a shared-ground check before plugging in the VE.Direct cable.
- Test plan step 3 compares the battery voltage with the 12 V bus (D1 monitor or a meter), not the Lithionics.
- Pretend MPPT (`--simulate`) now models a 12 V AGM bank and about 400 W of panels; web-page screenshots and the
  PDF guide rebuilt.

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
