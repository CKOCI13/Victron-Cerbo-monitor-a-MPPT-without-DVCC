"""Decoder for the VE.Direct text protocol of Victron MPPT solar chargers.

A VE.Direct device sends its readings once a second without being asked, as a block of
"<CR><LF><label><TAB><value>" fields that ends with a "Checksum" field. All bytes of a block,
including the checksum byte, add up to 0 modulo 256. The device can also send HEX-protocol
frames (":...<LF>") between the fields; those are skipped and left out of the checksum.

Source: Victron Energy, "VE.Direct Protocol" (text mode), and the field list of the
"BlueSolar / SmartSolar MPPT" VE.Direct text protocol. Units follow that document:
V, VPV and I in mV / mA, PPV in W, yields (H19/H20/H22) in 0.01 kWh.

This module has no dependencies and never writes anything: decoding only.
"""

# ------------------------------------------------------------------ tables

# CS: state of operation
STATES = {
    0: 'Off',
    1: 'Low power',
    2: 'Fault',
    3: 'Bulk',
    4: 'Absorption',
    5: 'Float',
    6: 'Storage',
    7: 'Equalize (manual)',
    9: 'Inverting',
    11: 'Power supply',
    245: 'Starting up',
    246: 'Repeated absorption',
    247: 'Auto equalize',
    248: 'BatterySafe',
    252: 'External control',
}

# ERR: error code (wording follows the MPPT solar charger manual, chapter 8.11)
ERRORS = {
    0: 'No error',
    1: 'Battery temperature too high',
    2: 'Battery voltage too high',
    17: 'Charger overheated',
    18: 'Charger over-current',
    19: 'Charger current reversed',
    20: 'Maximum bulk time exceeded',
    21: 'Current sensor issue',
    26: 'Terminals overheated',
    28: 'Power stage issue',
    33: 'PV over-voltage',
    34: 'PV over-current',
    38: 'PV input shutdown (battery over-voltage)',
    39: 'PV input shutdown (current while off)',
    40: 'PV input failed to shut down',
    65: 'Lost communication with a networked device',
    66: 'Networked device configuration issue',
    67: 'BMS connection lost',
    68: 'Network misconfigured',
    80: 'PV input shutdown',
    81: 'PV input shutdown',
    82: 'PV input shutdown',
    83: 'PV input shutdown',
    84: 'PV input shutdown',
    85: 'PV input shutdown',
    86: 'PV input shutdown',
    87: 'PV input shutdown',
    88: 'PV input shutdown',
    116: 'Calibration data lost',
    117: 'Incompatible firmware',
    119: 'Settings data lost',
}

# MPPT: tracker operation mode
TRACKER_MODES = {
    0: 'Off',
    1: 'Voltage or current limited',
    2: 'MPP tracking',
}

# OR: off reason (bit field)
OFF_REASONS = (
    (0x00000001, 'No input power'),
    (0x00000002, 'Switched off (power switch)'),
    (0x00000004, 'Switched off (device mode)'),
    (0x00000008, 'Remote input'),
    (0x00000010, 'Protection active'),
    (0x00000020, 'Pay-as-you-go'),
    (0x00000040, 'BMS'),
    (0x00000080, 'Engine shutdown detection'),
    (0x00000100, 'Analysing input voltage'),
)

# The fields this monitor decodes. Anything else is kept as raw text.
#   label: (key, kind, scale)
FIELDS = {
    'V': ('battery_voltage', 'int', 0.001),
    'I': ('battery_current', 'int', 0.001),
    'VPV': ('pv_voltage', 'int', 0.001),
    'PPV': ('pv_power', 'int', 1),
    'CS': ('state', 'int', 1),
    'MPPT': ('tracker_mode', 'int', 1),
    'OR': ('off_reason', 'hex', 1),
    'ERR': ('error', 'int', 1),
    'LOAD': ('load_on', 'onoff', 1),
    'IL': ('load_current', 'int', 0.001),
    'H19': ('yield_total', 'int', 0.01),
    'H20': ('yield_today', 'int', 0.01),
    'H21': ('max_power_today', 'int', 1),
    'H22': ('yield_yesterday', 'int', 0.01),
    'H23': ('max_power_yesterday', 'int', 1),
    'HSDS': ('day_sequence', 'int', 1),
    'T': ('temperature', 'int', 1),
    'PID': ('product_id', 'hex', 1),
    'SER#': ('serial', 'str', 1),
    'FW': ('firmware', 'fw', 1),
    'FWE': ('firmware', 'fwe', 1),
}

MAX_LABEL = 9          # longest label in the protocol is 8 characters
MAX_VALUE = 33         # longest value is 33 characters
MAX_FIELDS = 40        # a block has at most 22 fields; anything longer is noise


# ------------------------------------------------------------------ parser

class TextParser:
    """Byte-by-byte VE.Direct text parser.

    feed() takes raw bytes and returns the list of valid blocks (dicts of label -> text value)
    completed by them. Blocks whose checksum doesn't add up are counted in bad_blocks and dropped.
    Follows the reference state machine in Victron's VE.Direct protocol document.
    """

    WAIT_HEADER, IN_LABEL, IN_VALUE, IN_CHECKSUM, IN_HEX = range(5)

    def __init__(self):
        self.good_blocks = 0
        self.bad_blocks = 0
        self.hex_frames = 0
        self._reset()
        self._state = self.WAIT_HEADER
        self._before_hex = self.WAIT_HEADER

    def _reset(self):
        self._checksum = 0
        self._label = bytearray()
        self._value = bytearray()
        self._fields = {}

    def _garbage(self):
        """Line noise or a cut-off block: start again at the next field header."""
        self._reset()
        self._state = self.WAIT_HEADER

    def feed(self, data):
        blocks = []
        for b in bytes(data):
            if b == 0x3A and self._state not in (self.IN_CHECKSUM, self.IN_HEX):   # ':'
                self._before_hex = self._state
                self._state = self.IN_HEX
                continue
            if self._state == self.IN_HEX:
                if b == 0x0A:   # '\n' ends a HEX frame
                    self.hex_frames += 1
                    self._state = self._before_hex
                continue

            self._checksum = (self._checksum + b) & 0xFF
            state = self._state
            if state == self.WAIT_HEADER:
                if b == 0x0A:
                    self._state = self.IN_LABEL
                    self._label = bytearray()
                elif b != 0x0D:
                    self._garbage()
            elif state == self.IN_LABEL:
                if b == 0x09:   # tab
                    if bytes(self._label) == b'Checksum':
                        self._state = self.IN_CHECKSUM
                    else:
                        self._state = self.IN_VALUE
                        self._value = bytearray()
                elif len(self._label) >= MAX_LABEL or b < 0x20:
                    self._garbage()
                else:
                    self._label.append(b)
            elif state == self.IN_VALUE:
                if b == 0x0D:
                    if len(self._fields) >= MAX_FIELDS:
                        self._garbage()
                        continue
                    label = self._label.decode('ascii', 'replace')
                    self._fields[label] = self._value.decode('ascii', 'replace')
                    self._state = self.WAIT_HEADER
                elif len(self._value) >= MAX_VALUE or b == 0x0A:
                    self._garbage()
                else:
                    self._value.append(b)
            elif state == self.IN_CHECKSUM:
                # The checksum byte itself has just been added.
                if self._checksum == 0 and self._fields:
                    self.good_blocks += 1
                    blocks.append(self._fields)
                else:
                    self.bad_blocks += 1
                self._reset()
                self._state = self.WAIT_HEADER
        return blocks


def encode_block(fields):
    """Build a text block with a valid checksum from (label, value) pairs.

    Used by the simulation and the tests. Returns bytes.
    """
    body = b''.join(b'\r\n' + label.encode('ascii') + b'\t' + str(value).encode('ascii')
                    for label, value in fields)
    body += b'\r\nChecksum\t'
    check = (-sum(body)) & 0xFF
    return body + bytes((check,))


# ------------------------------------------------------------------ decoding

def _firmware(text, extended):
    """'161' -> '1.61'; FWE '0161FF' -> '1.61' (FF = release), '0162B3' -> '1.62-beta-B3'."""
    text = text.strip()
    if extended:
        if len(text) < 4 or not text[:4].isdigit():
            return text
        version = '%d.%s' % (int(text[:2]), text[2:4])
        tag = text[4:]
        return version if tag in ('', 'FF') else '%s-%s' % (version, tag)
    if text.isdigit() and len(text) >= 2:
        return '%d.%s' % (int(text[:-2] or '0'), text[-2:])
    return text


def decode(raw):
    """Turn a dict of raw label -> text into a dict of decoded values.

    Unknown labels are ignored; unparsable values become None.
    """
    out = {}
    for label, text in raw.items():
        spec = FIELDS.get(label)
        if spec is None:
            continue
        key, kind, scale = spec
        value = None
        t = text.strip()
        try:
            if kind == 'int':
                if t != '---':
                    n = int(t)
                    value = n if scale == 1 else round(n * scale, 3)
            elif kind == 'hex':
                value = int(t, 16)
            elif kind == 'onoff':
                value = {'ON': True, 'OFF': False}.get(t.upper())
            elif kind == 'fw':
                value = _firmware(t, False)
            elif kind == 'fwe':
                value = _firmware(t, True)
            else:
                value = t
        except ValueError:
            value = None
        if kind in ('fw', 'fwe') and key in out and label == 'FW':
            continue   # FWE (more precise) wins over FW when both are sent
        out[key] = value
    return out


def state_text(state):
    if state is None:
        return None
    return STATES.get(state, 'Unknown state %d' % state)


def error_text(error):
    if error is None:
        return None
    return ERRORS.get(error, 'Error %d' % error)


def tracker_text(mode):
    if mode is None:
        return None
    return TRACKER_MODES.get(mode, 'Unknown (%d)' % mode)


def off_reason_texts(off_reason):
    if not off_reason:
        return []
    texts = [text for bit, text in OFF_REASONS if off_reason & bit]
    known = 0
    for bit, _ in OFF_REASONS:
        known |= bit
    if off_reason & ~known:
        texts.append('Other (0x%08X)' % (off_reason & ~known))
    return texts


def battery_power(values):
    """Battery-side power in W (V x I), or None."""
    v, i = values.get('battery_voltage'), values.get('battery_current')
    if v is None or i is None:
        return None
    return round(v * i)
