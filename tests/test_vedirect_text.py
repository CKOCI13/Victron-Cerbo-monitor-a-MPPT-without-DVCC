"""Tests for vedirect_text.py: the VE.Direct text-protocol parser and decoder."""

import unittest

from support import vt, MPPT_BLOCK, MPPT_FIELDS


def checksum_ok(data):
    return sum(data) & 0xFF == 0


class ParserTest(unittest.TestCase):
    def test_encoded_block_has_a_valid_checksum(self):
        self.assertTrue(checksum_ok(MPPT_BLOCK))
        self.assertTrue(MPPT_BLOCK.startswith(b'\r\nPID\t0xA060'))
        self.assertIn(b'\r\nChecksum\t', MPPT_BLOCK)

    def test_hand_worked_block(self):
        # Worked out by hand: CR LF V TAB = 13+10+86+9 = 118; "12800" = 49+50+56+48+48 = 251;
        # CR LF = 23; "Checksum" = 819; TAB = 9. Total 1220 = 4 x 256 + 196, so the checksum
        # byte must be 256 - 196 = 60 (0x3C) to bring the sum to 0 modulo 256.
        body = b'\r\nV\t12800\r\nChecksum\t'
        self.assertEqual(sum(body), 1220)
        self.assertEqual(vt.encode_block((('V', '12800'),)), body + b'\x3c')
        p = vt.TextParser()
        self.assertEqual(p.feed(body + b'\x3c'), [{'V': '12800'}])

    def test_one_block(self):
        p = vt.TextParser()
        blocks = p.feed(MPPT_BLOCK)
        self.assertEqual(blocks, [dict(MPPT_FIELDS)])
        self.assertEqual((p.good_blocks, p.bad_blocks), (1, 0))

    def test_byte_by_byte_and_back_to_back(self):
        p = vt.TextParser()
        blocks = []
        for b in MPPT_BLOCK * 3:
            blocks += p.feed(bytes((b,)))
        self.assertEqual(len(blocks), 3)
        self.assertEqual(p.bad_blocks, 0)

    def test_starting_in_the_middle_of_a_block(self):
        # The port is opened at a random moment: the first, cut-off block is dropped, the next is good.
        p = vt.TextParser()
        blocks = p.feed(MPPT_BLOCK[40:] + MPPT_BLOCK)
        self.assertEqual(blocks, [dict(MPPT_FIELDS)])

    def test_bad_checksum_is_dropped(self):
        bad = bytearray(MPPT_BLOCK)
        bad[10] ^= 0x01
        p = vt.TextParser()
        self.assertEqual(p.feed(bytes(bad)), [])
        self.assertEqual(p.bad_blocks, 1)
        self.assertEqual(len(p.feed(MPPT_BLOCK)), 1)   # and it recovers

    def test_hex_frames_are_skipped_and_left_out_of_the_checksum(self):
        half = len(MPPT_BLOCK) // 2
        # cut at a field boundary so the HEX frame sits between fields
        cut = MPPT_BLOCK.index(b'\r\n', half)
        data = MPPT_BLOCK[:cut] + b':A0102000543\n' + MPPT_BLOCK[cut:]
        p = vt.TextParser()
        self.assertEqual(p.feed(b':154\n' + data), [dict(MPPT_FIELDS)])
        self.assertEqual(p.hex_frames, 2)

    def test_checksum_byte_that_looks_like_a_hex_start(self):
        # Find a block whose checksum byte is ':' (0x3A) and check it isn't taken for a HEX frame.
        fields = next(f for f in ((('V', str(n)), ('I', str(m))) for n in range(300) for m in range(300))
                      if vt.encode_block(f)[-1] == 0x3A)
        block = vt.encode_block(fields)
        self.assertEqual(vt.TextParser().feed(block + MPPT_BLOCK), [dict(fields), dict(MPPT_FIELDS)])

    def test_noise_does_not_grow_memory_or_crash(self):
        p = vt.TextParser()
        p.feed(b'\r\n' + b'X' * 5000 + b'\t' + b'Y' * 5000 + b'\r')
        p.feed(bytes(range(256)) * 20)
        self.assertLessEqual(len(p._label), vt.MAX_LABEL)
        self.assertLessEqual(len(p._value), vt.MAX_VALUE)
        # At most the first block after the noise is lost.
        self.assertGreaterEqual(len(p.feed(MPPT_BLOCK * 3)), 2)


class DecodeTest(unittest.TestCase):
    def setUp(self):
        self.v = vt.decode(dict(MPPT_FIELDS))

    def test_units(self):
        v = self.v
        self.assertEqual(v['battery_voltage'], 53.12)
        self.assertEqual(v['battery_current'], 12.3)
        self.assertEqual(v['pv_voltage'], 98.76)
        self.assertEqual(v['pv_power'], 670)
        self.assertEqual(v['yield_total'], 1234.56)
        self.assertEqual(v['yield_today'], 3.21)
        self.assertEqual(v['yield_yesterday'], 4.12)
        self.assertEqual(v['max_power_today'], 812)
        self.assertEqual(v['max_power_yesterday'], 905)

    def test_identity(self):
        self.assertEqual(self.v['product_id'], 0xA060)
        self.assertEqual(self.v['serial'], 'HQ2245ABCDE')
        self.assertEqual(self.v['firmware'], '1.64')

    def test_state_and_texts(self):
        self.assertEqual(self.v['state'], 3)
        self.assertEqual(vt.state_text(3), 'Bulk')
        self.assertEqual(vt.state_text(252), 'External control')
        self.assertEqual(vt.state_text(99), 'Unknown state 99')
        self.assertEqual(vt.error_text(0), 'No error')
        self.assertEqual(vt.error_text(67), 'BMS connection lost')
        self.assertEqual(vt.error_text(200), 'Error 200')
        self.assertEqual(vt.tracker_text(2), 'MPP tracking')
        self.assertIs(self.v['load_on'], False)

    def test_off_reasons(self):
        self.assertEqual(vt.off_reason_texts(0), [])
        self.assertEqual(vt.off_reason_texts(0x00000001), ['No input power'])
        self.assertEqual(vt.off_reason_texts(0x00000041), ['No input power', 'BMS'])
        self.assertEqual(vt.off_reason_texts(0x80000000), ['Other (0x80000000)'])

    def test_negative_current_and_missing_values(self):
        v = vt.decode({'I': '-1500', 'IL': '---', 'V': 'junk'})
        self.assertEqual(v['battery_current'], -1.5)
        self.assertIsNone(v['load_current'])
        self.assertIsNone(v['battery_voltage'])

    def test_firmware_forms(self):
        self.assertEqual(vt.decode({'FW': '161'})['firmware'], '1.61')
        self.assertEqual(vt.decode({'FW': '1161'})['firmware'], '11.61')
        self.assertEqual(vt.decode({'FWE': '0161FF'})['firmware'], '1.61')
        self.assertEqual(vt.decode({'FWE': '0162B3'})['firmware'], '1.62-B3')
        # FWE is more precise: it wins whichever order the two arrive in
        self.assertEqual(vt.decode({'FWE': '0162B3', 'FW': '162'})['firmware'], '1.62-B3')
        self.assertEqual(vt.decode({'FW': '162', 'FWE': '0162B3'})['firmware'], '1.62-B3')

    def test_power(self):
        self.assertEqual(vt.battery_power(self.v), 653)   # 53.12 V x 12.3 A
        self.assertIsNone(vt.battery_power({}))

    def test_unknown_labels_are_ignored(self):
        self.assertEqual(vt.decode({'XYZ': '1'}), {})


if __name__ == '__main__':
    unittest.main()
