import math
from pathlib import Path
import struct
import tempfile
import unittest

from ch10viewer.reader import FormatError, PRIMARY, extract, read_data, scan


def packet(data=b'\x00\x01\x02\x03', secondary=False, channel=7, rtc=100):
    flags = 0x80 if secondary else 0
    header_length = 36 if secondary else 24
    padding = b'\x00' * (-(header_length + len(data)) % 4)
    raw = PRIMARY.pack(0xEB25, channel, header_length + len(data) + len(padding),
                       len(data), 0xFF, 3, flags, 0x68, rtc.to_bytes(6, 'little'), 0)
    checksum = sum(struct.unpack('<11H', raw[:22])) & 0xFFFF
    return raw[:22] + struct.pack('<H', checksum) + (b'\x00' * 12 if secondary else b'') + data + padding


class ReaderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'synthetic.ch10'

    def write(self, data):
        self.path.write_bytes(data)
        return scan(self.path)

    def test_index_secondary_header_and_filler(self):
        packets = self.write(packet(b'abc') + packet(b'defgh', True, rtc=2**48 - 1))
        self.assertEqual(len(packets), 2)
        self.assertEqual(packets[1].offset, 28)
        self.assertEqual(packets[1].rtc, 2**48 - 1)
        self.assertTrue(all(p.checksum_ok for p in packets))
        self.assertEqual(read_data(self.path, packets[1]), b'defgh')

    def test_big_endian_scaled_scalar_and_channel_filter(self):
        packets = self.write(packet(b'AA' + struct.pack('>h', -120)) + packet(channel=8))
        rows, skipped = extract(self.path, packets, offset=2, kind='int16', endian='big',
                                scale=.5, bias=2, channel=7)
        self.assertEqual(rows, [(0, 7, 100, -58)])
        self.assertEqual(skipped, 0)

    def test_short_data_and_nan_are_reported(self):
        packets = self.write(packet(struct.pack('<f', math.nan)) + packet(b'a'))
        rows, skipped = extract(self.path, packets, offset=0, kind='float32')
        self.assertEqual(rows, [])
        self.assertEqual(skipped, 2)

    def test_checksum_corruption_is_visible(self):
        raw = bytearray(packet())
        raw[2] ^= 1
        self.assertFalse(self.write(raw)[0].checksum_ok)

    def test_truncation_invalid_lengths_and_sync(self):
        raw = packet()
        for bad in (b'', raw[:10], raw[:-1], b'XX' + raw[2:],
                    raw[:4] + struct.pack('<I', 25) + raw[8:],
                    raw[:8] + struct.pack('<I', 99) + raw[12:]):
            with self.subTest(bad=bad):
                self.path.write_bytes(bad)
                with self.assertRaises(FormatError):
                    scan(self.path)

    def test_invalid_field_configuration(self):
        packets = self.write(packet())
        for params in ({'offset': -1, 'kind': 'uint8'},
                       {'offset': 0, 'kind': 'wrong'},
                       {'offset': 0, 'kind': 'uint8', 'scale': math.inf}):
            with self.assertRaises(ValueError):
                extract(self.path, packets, **params)


if __name__ == '__main__':
    unittest.main()
